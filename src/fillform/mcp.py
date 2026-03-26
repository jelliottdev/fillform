"""Real MCP server for fillform.

This server exposes the form-analysis pipeline as MCP tools so that a running
Claude Code session can analyse PDF forms **without making any additional API
calls**.  Claude receives annotated page images directly as tool-response content
and performs the vision analysis in its own context.

Tools
-----
fillform_workflow_guide
    Returns a compact quickstart and tool-call templates for agents.

analyze_form
    One-shot form understanding: extracts fields, guesses semantic labels,
    returns confidence scores, and highlights ambiguous fields.

extract_form_fields
    Extract AcroForm fields, assign FXXX aliases, render annotated pages, and
    return the alias map JSON + one PNG image per page.  Claude reads the images
    and identifies what each labeled field is for.

save_field_mapping
    Accepts Claude's field analysis (as JSON), combines it with the alias map,
    builds a :class:`~fillform.contracts.CanonicalSchema`, persists the schema
    and a plain-text fill script, and returns the fill script text.

prepare_form_for_analysis
    Backward-compatible alias for ``extract_form_fields``.

fill_pdf_form
    Fill a PDF with user-provided values keyed by FXXX alias or raw field name.

complete_form
    High-level pipeline: analyze + (optional demo data generation) + fill +
    completion report in one call.

fill_form
    Semantic fill helper that accepts business-level keys and auto-maps them to
    aliases/field names using label guesses.

validate_form
    Lightweight post-fill QA that reports unresolved and potentially empty fields.

Usage
-----
**stdio** (local process, Claude Code default)::

    python -m fillform.mcp

    # ~/.claude/settings.json
    {
      "mcpServers": {
        "fillform": {
          "command": "python",
          "args": ["-m", "fillform.mcp"],
          "cwd": "/path/to/fillform/src"
        }
      }
    }

**HTTP / SSE** (URL-based, works with remote Claude Code or any MCP client)::

    python -m fillform.mcp --http              # listens on http://localhost:8000/sse
    python -m fillform.mcp --http --port 9000  # custom port
    python -m fillform.mcp --http --host 0.0.0.0 --port 8000  # public

    # ~/.claude/settings.json
    {
      "mcpServers": {
        "fillform": {
          "url": "http://localhost:8000/sse"
        }
      }
    }
"""

from __future__ import annotations

import base64
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any, Sequence

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server
from mcp.types import (
    ImageContent,
    TextContent,
    Tool,
)

from .annotator import PdfAnnotator
from .contracts import CanonicalField, CanonicalSchema
from .field_alias import AliasMap, FieldAliasRegistry
from .mcp_support import (
    PDF_BYTES_DESCRIPTION,
    create_session,
    get_session,
    pdf_source_properties,
    resolve_pdf_source,
)
from .structure import PdfStructureService, TextBlock

# ---------------------------------------------------------------------------
# Server singleton
# ---------------------------------------------------------------------------

server = Server("fillform")


def _make_structure_service() -> PdfStructureService:
    """Prefer PyMuPDF; fall back to pypdf if fitz is unavailable."""
    try:
        return PdfStructureService(provider="pymupdf")
    except Exception:
        return PdfStructureService(provider="pypdf")


# Module-level service instances (lightweight, no I/O at import time)
_structure_service = _make_structure_service()
_alias_registry = FieldAliasRegistry()
_annotator = PdfAnnotator()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="fillform_workflow_guide",
            description=(
                "Start here for FillForm usage. Returns the recommended tool order "
                "and copy/paste-ready payload templates."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="analyze_form",
            description=(
                "One-shot semantic analysis for an AcroForm PDF. Returns field-level "
                "label guesses, confidence scores, section hints, and an ambiguity list."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **pdf_source_properties("Absolute or relative path to the PDF file."),
                    "annotate_pages": {
                        "type": "boolean",
                        "description": "Default false. Set true to attach annotated page images.",
                        "default": False,
                    },
                    "ambiguity_threshold": {
                        "type": "number",
                        "description": "Confidence below this value is flagged as ambiguous (0..1). Default 0.72.",
                        "default": 0.72,
                    },
                },
                "required": ["pdf_path"],
            },
        ),
        Tool(
            name="extract_form_fields",
            description=(
                "Extract AcroForm fields from a PDF, assign sequential FXXX aliases "
                "(F001, F002, …), and return a compact JSON object with the alias map "
                "and per-field metadata (page, bbox, type, nearby text label). "
                "The response is JSON-only by default — no images — keeping context usage minimal. "
                "Set annotate_pages=true to also receive annotated page images when "
                "nearby_text labels are insufficient to identify fields."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **pdf_source_properties("Absolute or relative path to the PDF file."),
                    "annotate_pages": {
                        "type": "boolean",
                        "description": "Default false. Set true to receive annotated JPEG page images alongside the JSON.",
                        "default": False,
                    },
                    "persist_session": {
                        "type": "boolean",
                        "description": "Default true. Persist alias map/pdf path in server memory and return a session_id for follow-up calls.",
                        "default": True,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="prepare_form_for_analysis",
            description=(
                "Alias for extract_form_fields (same inputs/outputs). "
                "Use this if your agent expects the older tool name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pdf_path": {
                        "type": "string",
                        "description": "Absolute or relative path to the PDF file.",
                    },
                    "annotate_pages": {
                        "type": "boolean",
                        "description": "Default false. Set true to receive annotated JPEG page images alongside the JSON.",
                        "default": False,
                    },
                },
                "required": ["pdf_path"],
            },
        ),
        Tool(
            name="save_field_mapping",
            description=(
                "Persist the field analysis produced by Claude into a CanonicalSchema "
                "JSON file and a plain-text AI fill guide (fill script). Returns the "
                "full fill script text so you can review it immediately."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pdf_path": {
                        "type": "string",
                        "description": "Path to the original PDF (used to derive default output paths).",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional: session_id returned by extract_form_fields/analyze_form to avoid re-sending alias_map_json.",
                    },
                    "alias_map_json": {
                        "description": (
                            "Alias map from extract_form_fields. Accepts either a JSON string "
                            "or an object with alias→field-name entries."
                        ),
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object"},
                        ],
                    },
                    "field_analysis_json": {
                        "description": (
                            "JSON object mapping each FXXX alias to its semantic description. "
                            "Accepts either a JSON string or an object."
                        ),
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object"},
                        ],
                    },
                    "form_family": {
                        "type": "string",
                        "description": "Logical form family name (e.g. 'W-9', 'I-9', 'intake_form').",
                        "default": "unknown",
                    },
                    "version": {
                        "type": "string",
                        "description": "Schema version string.",
                        "default": "1",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": (
                            "Directory to write schema.json and fill_script.md. "
                            "Defaults to the same directory as the PDF."
                        ),
                    },
                },
                "required": ["field_analysis_json"],
            },
        ),
        Tool(
            name="fill_pdf_form",
            description=(
                "Fill an AcroForm PDF using user-provided values keyed by either FXXX alias "
                "or raw PDF field names. Returns the output PDF path and a per-field fill log."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **pdf_source_properties("Absolute or relative path to source PDF."),
                    "values_json": {
                        "description": (
                            "Field values to apply. Accepts either a JSON string or an object. "
                            "Keys may be FXXX aliases or raw PDF field names."
                        ),
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object"},
                        ],
                    },
                    "alias_map_json": {
                        "description": (
                            "Optional alias map from extract_form_fields (JSON string or object). "
                            "Required when values_json uses FXXX aliases."
                        ),
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object"},
                        ],
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional: session_id returned by extract_form_fields/analyze_form.",
                    },
                    "output_pdf_path": {
                        "type": "string",
                        "description": (
                            "Optional output path. Defaults to <input_stem>_filled.pdf "
                            "next to the source PDF."
                        ),
                    },
                },
                "required": ["values_json"],
            },
        ),
        Tool(
            name="fill_this_pdf",
            description=(
                "Alias for fill_pdf_form. Use this if your agent expects a plain-language "
                "\"fill this PDF\" action."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **pdf_source_properties("Absolute or relative path to source PDF."),
                    "values_json": {
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                        "description": "Alias/value or field-name/value mapping.",
                    },
                    "alias_map_json": {
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                        "description": "Optional alias map for FXXX keys.",
                    },
                    "session_id": {"type": "string"},
                    "output_pdf_path": {"type": "string"},
                },
                "required": ["values_json"],
            },
        ),
        Tool(
            name="complete_form",
            description=(
                "One-call pipeline for analyze + fill + completion report. "
                "Use mode='demo' to auto-generate believable placeholder values."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **pdf_source_properties("Absolute or relative path to source PDF."),
                    "mode": {
                        "type": "string",
                        "description": "Either 'user_data' (default) or 'demo'.",
                        "default": "user_data",
                    },
                    "data_json": {
                        "description": (
                            "Optional semantic/alias values as JSON string or object. "
                            "When omitted and mode='demo', the tool generates demo values."
                        ),
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                    },
                    "output_pdf_path": {
                        "type": "string",
                        "description": "Optional output path for the filled PDF.",
                    },
                    "preview_pages": {
                        "type": "boolean",
                        "description": "Default false. Attach filled-page preview images.",
                        "default": False,
                    },
                    "strict_validation": {
                        "type": "boolean",
                        "description": "Default true. Mark completion as partial when logic issues are detected.",
                        "default": True,
                    },
                    "auto_fix_logic": {
                        "type": "boolean",
                        "description": "Default true. Apply one repair pass to clear contradictory conditional text.",
                        "default": True,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="one_shot_fill_form",
            description=(
                "Fast one-shot endpoint: analyze + fill + validate + auto-fix. "
                "Use mode='demo' for high-quality synthetic form completion with minimal orchestration."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **pdf_source_properties("Absolute or relative path to source PDF."),
                    "mode": {
                        "type": "string",
                        "description": "Either 'user_data' or 'demo'. Default 'demo'.",
                        "default": "demo",
                    },
                    "data_json": {
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                        "description": "Optional payload when mode='user_data'.",
                    },
                    "output_pdf_path": {"type": "string"},
                    "preview_pages": {"type": "boolean", "default": False},
                },
                "required": [],
            },
        ),
        Tool(
            name="fill_form",
            description=(
                "Fill using semantic keys (for example 'full_name', 'filing_status'). "
                "The tool auto-maps keys to aliases/field names and returns mapping confidence."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **pdf_source_properties("Absolute or relative path to source PDF."),
                    "semantic_data_json": {
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                        "description": "Semantic key/value payload.",
                    },
                    "output_pdf_path": {"type": "string"},
                },
                "required": ["semantic_data_json"],
            },
        ),
        Tool(
            name="validate_form",
            description=(
                "Run a lightweight validation pass on a filled PDF and report likely "
                "empty fields and field coverage counts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **pdf_source_properties("Absolute or relative path to source PDF."),
                    "expected_min_fill_ratio": {
                        "type": "number",
                        "default": 0.6,
                        "description": "Warn if less than this ratio of fields appear populated.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="map_fill_validate",
            description=(
                "One-call orchestration: analyze + fill + validate. "
                "Use mode='demo' to auto-generate believable demo values."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **pdf_source_properties("Absolute or relative path to source PDF."),
                    "mode": {
                        "type": "string",
                        "description": "Either 'user_data' (default) or 'demo'.",
                        "default": "user_data",
                    },
                    "data_json": {
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                    },
                    "output_pdf_path": {"type": "string"},
                    "preview_pages": {"type": "boolean", "default": False},
                    "expected_min_fill_ratio": {"type": "number", "default": 0.6},
                },
                "required": [],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent | ImageContent]:
    if name == "fillform_workflow_guide":
        return _workflow_guide()
    if name == "analyze_form":
        return await _analyze_form(arguments)
    if name == "extract_form_fields":
        return await _extract_fields(arguments)
    if name == "prepare_form_for_analysis":
        return await _extract_fields(arguments)
    if name == "save_field_mapping":
        return await _save_mapping(arguments)
    if name == "fill_pdf_form":
        return await _fill_pdf_form(arguments)
    if name == "fill_this_pdf":
        return await _fill_pdf_form(arguments)
    if name == "complete_form":
        return await _complete_form(arguments)
    if name == "one_shot_fill_form":
        one_shot_args = dict(arguments)
        one_shot_args.setdefault("mode", "demo")
        one_shot_args.setdefault("strict_validation", True)
        one_shot_args.setdefault("auto_fix_logic", True)
        return await _complete_form(one_shot_args)
    if name == "fill_form":
        return await _fill_form(arguments)
    if name == "validate_form":
        return await _validate_form(arguments)
    if name == "map_fill_validate":
        return await _map_fill_validate(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _extract_fields(args: dict[str, Any]) -> list[TextContent | ImageContent]:
    try:
        pdf_path = resolve_pdf_source(args)
    except ValueError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]
    annotate_pages = bool(args.get("annotate_pages", False))

    if not pdf_path.exists():
        return [TextContent(type="text", text=f"ERROR: File not found: {pdf_path}")]

    try:
        structure = _structure_service.extract(pdf_path)
    except Exception as exc:
        return [TextContent(type="text", text=f"ERROR extracting structure: {exc}")]

    if not structure.field_widgets:
        return [TextContent(type="text", text=(
            f"No AcroForm fields found in '{pdf_path.name}'. "
            "This pipeline requires a PDF with interactive AcroForm fields."
        ))]

    alias_map = _alias_registry.assign(structure.field_widgets)

    # Build per-field metadata with nearby text labels
    fields_data: list[dict[str, Any]] = []
    for alias, widget in alias_map.field_widgets.items():
        nearby = _find_nearby_text(widget.bbox, widget.page, structure.text_blocks)
        page_dim = next(
            (pd for pd in structure.page_dimensions if pd.page == widget.page), None
        )
        position = _position_hint(widget.bbox, widget.page, page_dim)
        fields_data.append({
            "alias": alias,
            "name": widget.name,
            "type": widget.field_type,
            "page": widget.page + 1,
            "bbox": [round(v, 1) for v in widget.bbox],
            "position": position,
            "nearby_text": nearby,
        })

    fields_data.sort(key=lambda f: f["alias"])

    result: dict[str, Any] = {
        "recommended_tool_order": [
            "fillform_workflow_guide",
            "extract_form_fields (or prepare_form_for_analysis)",
            "save_field_mapping",
            "fill_pdf_form",
        ],
        "field_count": len(fields_data),
        "page_count": len(structure.page_dimensions),
        "alias_map": alias_map.alias_to_field,
        "alias_map_json": json.dumps(alias_map.alias_to_field),
        "fields": fields_data,
        "instructions": (
            "Review the 'nearby_text' and 'position' for each field to identify what it collects. "
            "Once all fields are identified, call save_field_mapping with your analysis."
        ),
        "next_call_templates": {
            "save_field_mapping": {
                "session_id": "<optional session_id>",
                "alias_map_json": "<paste alias_map_json>",
                "field_analysis_json": "{\"F001\": {\"label\": \"...\", \"context\": \"...\", \"expected_value_type\": \"string\", \"expected_format\": null, \"is_required\": false, \"section\": null}}",
                "form_family": "unknown",
                "version": "1",
            },
            "fill_pdf_form": {
                "pdf_path": str(pdf_path),
                "values_json": "{\"F001\": \"value\"}",
                "session_id": "<optional session_id>",
                "alias_map_json": "<paste alias_map_json when using FXXX keys>",
            },
        },
    }
    if bool(args.get("persist_session", True)):
        session_id = create_session(
            pdf_path=pdf_path,
            alias_map=alias_map.alias_to_field,
        )
        result["session_id"] = session_id
        result["session_expires_note"] = "In-memory session, valid while this MCP process is alive."

    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(result, indent=2))
    ]

    if annotate_pages:
        tmp = tempfile.NamedTemporaryFile(suffix="_annotated.pdf", delete=False)
        tmp.close()
        annotated_path = Path(tmp.name)
        try:
            _annotator.annotate(pdf_path, alias_map, annotated_path)
            page_images = _render_pages(annotated_path)
            total = len(page_images)
            for i, img_b64 in enumerate(page_images):
                content.append(TextContent(type="text", text=f"--- Page {i+1} of {total} ---"))
                content.append(ImageContent(type="image", data=img_b64, mimeType="image/jpeg"))
        except Exception as exc:
            content.append(TextContent(type="text", text=f"WARNING: annotation failed: {exc}"))
        finally:
            annotated_path.unlink(missing_ok=True)

    return content


async def _analyze_form(args: dict[str, Any]) -> list[TextContent | ImageContent]:
    try:
        pdf_path = resolve_pdf_source(args)
    except ValueError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]
    annotate_pages = bool(args.get("annotate_pages", False))
    threshold_raw = args.get("ambiguity_threshold", 0.72)
    try:
        ambiguity_threshold = float(threshold_raw)
    except (TypeError, ValueError):
        ambiguity_threshold = 0.72
    ambiguity_threshold = max(0.0, min(1.0, ambiguity_threshold))

    if not pdf_path.exists():
        return [TextContent(type="text", text=f"ERROR: File not found: {pdf_path}")]

    try:
        structure = _structure_service.extract(pdf_path)
    except Exception as exc:
        return [TextContent(type="text", text=f"ERROR extracting structure: {exc}")]

    if not structure.field_widgets:
        return [TextContent(type="text", text=(
            f"No AcroForm fields found in '{pdf_path.name}'. "
            "This pipeline requires a PDF with interactive AcroForm fields."
        ))]

    alias_map = _alias_registry.assign(structure.field_widgets)
    analysis_fields: list[dict[str, Any]] = []
    ambiguous_fields: list[dict[str, Any]] = []

    for alias, widget in sorted(alias_map.field_widgets.items()):
        nearby = _find_nearby_text(widget.bbox, widget.page, structure.text_blocks)
        page_dim = next((pd for pd in structure.page_dimensions if pd.page == widget.page), None)
        section_hint = _section_hint(widget.page, widget.bbox, structure.text_blocks)
        label_guess, confidence, rationale = _guess_semantics(
            field_name=widget.name,
            nearby_text=nearby,
            field_type=widget.field_type,
        )
        entry = {
            "alias": alias,
            "field_name": widget.name,
            "field_type": widget.field_type,
            "page": widget.page + 1,
            "bbox": [round(v, 1) for v in widget.bbox],
            "position": _position_hint(widget.bbox, widget.page, page_dim),
            "label_guess": label_guess,
            "section_hint": section_hint,
            "confidence": round(confidence, 3),
            "rationale": rationale,
            "nearby_text": nearby,
        }
        analysis_fields.append(entry)
        if confidence < ambiguity_threshold:
            ambiguous_fields.append(
                {
                    "alias": alias,
                    "confidence": round(confidence, 3),
                    "label_guess": label_guess,
                    "suggested_review": "Confirm this field manually from the annotated page image.",
                }
            )

    result: dict[str, Any] = {
        "pdf_path": str(pdf_path),
        "field_count": len(analysis_fields),
        "alias_map": alias_map.alias_to_field,
        "alias_map_json": json.dumps(alias_map.alias_to_field),
        "ambiguity_threshold": ambiguity_threshold,
        "ambiguous_count": len(ambiguous_fields),
        "ambiguous_fields": ambiguous_fields,
        "fields": analysis_fields,
        "next_actions": {
            "save_field_mapping": "Use alias_map_json + your verified field analysis.",
            "fill_pdf_form": "After collecting user data, pass values_json and alias_map_json.",
        },
    }
    if bool(args.get("persist_session", True)):
        session_id = create_session(pdf_path=pdf_path, alias_map=alias_map.alias_to_field)
        result["session_id"] = session_id
        result["session_expires_note"] = "In-memory session, valid while this MCP process is alive."

    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(result, indent=2))
    ]
    if annotate_pages:
        tmp = tempfile.NamedTemporaryFile(suffix="_annotated.pdf", delete=False)
        tmp.close()
        annotated_path = Path(tmp.name)
        try:
            _annotator.annotate(pdf_path, alias_map, annotated_path)
            for i, img_b64 in enumerate(_render_pages(annotated_path), start=1):
                content.append(TextContent(type="text", text=f"--- Analyze Form Page {i} ---"))
                content.append(ImageContent(type="image", data=img_b64, mimeType="image/jpeg"))
        except Exception as exc:
            content.append(TextContent(type="text", text=f"WARNING: annotation failed: {exc}"))
        finally:
            annotated_path.unlink(missing_ok=True)
    return content


async def _save_mapping(args: dict[str, Any]) -> list[TextContent]:
    session = get_session(args.get("session_id"))
    pdf_path_str = args.get("pdf_path") or (session.get("pdf_path") if session else "")
    form_family = str(args.get("form_family") or "unknown")
    version = str(args.get("version") or "1")

    if pdf_path_str:
        pdf_path = Path(pdf_path_str).expanduser().resolve()
        output_dir = Path(args["output_dir"]).expanduser().resolve() if args.get("output_dir") else pdf_path.parent
    else:
        output_dir = Path(args.get("output_dir") or "/tmp").expanduser().resolve()

    # Parse alias map
    alias_map_raw: dict[str, Any]
    if args.get("alias_map_json") is None and session:
        alias_map_raw = dict(session["alias_map"])
    else:
        try:
            alias_map_raw = _coerce_json_object(args.get("alias_map_json"), "alias_map_json")
        except ValueError as exc:
            return [TextContent(type="text", text=f"ERROR: {exc}")]

    # Parse field analysis
    try:
        field_analysis = _coerce_json_object(args.get("field_analysis_json"), "field_analysis_json")
    except ValueError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]

    # Accept both flat {alias→name} and nested {alias_index: {alias→name}} formats
    if "alias_index" in alias_map_raw:
        alias_index: dict[str, str] = alias_map_raw["alias_index"]
    else:
        alias_index = {k: v for k, v in alias_map_raw.items() if isinstance(v, str)}

    # Build CanonicalFields
    fields: list[CanonicalField] = []
    for alias in sorted(alias_index.keys()):
        field_name = alias_index[alias]
        data = field_analysis.get(alias) or {}
        fields.append(
            CanonicalField(
                alias=alias,
                field_name=field_name,
                field_type=data.get("field_type") or "unknown",
                page=int(data.get("page") or 0),
                bbox=tuple(data.get("bbox") or (0.0, 0.0, 0.0, 0.0)),  # type: ignore[arg-type]
                label=data.get("label"),
                context=data.get("context"),
                expected_value_type=data.get("expected_value_type"),
                expected_format=data.get("expected_format"),
                is_required=bool(data.get("is_required", False)),
                section=data.get("section"),
            )
        )

    schema = CanonicalSchema(
        form_family=form_family,
        version=version,
        mode="acroform",
        fields=fields,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    schema_path = output_dir / f"{form_family}_schema_v{version}.json"
    fill_script_path = output_dir / f"{form_family}_fill_script_v{version}.md"

    schema_path.write_text(json.dumps(schema.to_dict(), indent=2))
    fill_script = schema.to_fill_script()
    fill_script_path.write_text(fill_script)

    return [
        TextContent(
            type="text",
            text=(
                f"Saved {len(fields)} fields.\n"
                f"  Schema      : {schema_path}\n"
                f"  Fill script : {fill_script_path}\n\n"
                f"{'─' * 60}\n"
                f"{fill_script}"
            ),
        )
    ]


async def _fill_pdf_form(args: dict[str, Any]) -> list[TextContent]:
    session = get_session(args.get("session_id"))
    try:
        pdf_path = resolve_pdf_source(args, default_path=(session.get("pdf_path") if session else None))
    except ValueError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]
    if not pdf_path.exists():
        return [TextContent(type="text", text=f"ERROR: File not found: {pdf_path}")]

    output_pdf_path = Path(
        str(args.get("output_pdf_path") or (pdf_path.parent / f"{pdf_path.stem}_filled.pdf"))
    ).expanduser().resolve()

    try:
        values = _coerce_json_object(args.get("values_json"), "values_json")
    except ValueError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]

    alias_map: dict[str, str] = {}
    if args.get("alias_map_json") is not None:
        try:
            alias_map_raw = _coerce_json_object(args.get("alias_map_json"), "alias_map_json")
            if "alias_index" in alias_map_raw and isinstance(alias_map_raw["alias_index"], dict):
                alias_map = {str(k): str(v) for k, v in alias_map_raw["alias_index"].items()}
            else:
                alias_map = {str(k): str(v) for k, v in alias_map_raw.items()}
        except ValueError as exc:
            return [TextContent(type="text", text=f"ERROR: {exc}")]
    elif session:
        alias_map = dict(session["alias_map"])

    try:
        import fitz
    except ImportError as exc:
        return [TextContent(type="text", text=f"ERROR: PyMuPDF (fitz) is required: {exc}")]

    fill_report = _fill_pdf_document_report(
        pdf_path=pdf_path,
        output_pdf_path=output_pdf_path,
        values=values,
        alias_map=alias_map,
    )
    fill_log = fill_report["fill_log"]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "source_pdf": str(pdf_path),
                    "output_pdf": str(output_pdf_path),
                    "filled": sum(1 for v in fill_log.values() if v.startswith("ok:")),
                    "total_values": len(values),
                    "fill_log": fill_log,
                    "changed_fields": fill_report["changed_fields"],
                },
                indent=2,
            ),
        )
    ]


async def _complete_form(args: dict[str, Any]) -> list[TextContent | ImageContent]:
    session = get_session(args.get("session_id"))
    try:
        pdf_path = resolve_pdf_source(args, default_path=(session.get("pdf_path") if session else None))
    except ValueError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]
    if not pdf_path.exists():
        return [TextContent(type="text", text=f"ERROR: File not found: {pdf_path}")]

    mode = str(args.get("mode") or "user_data").strip().lower()
    if mode not in {"user_data", "demo"}:
        mode = "user_data"

    preview_pages = bool(args.get("preview_pages", False))
    strict_validation = bool(args.get("strict_validation", True))
    auto_fix_logic = bool(args.get("auto_fix_logic", True))
    output_pdf_path = Path(
        str(args.get("output_pdf_path") or (pdf_path.parent / f"{pdf_path.stem}_completed.pdf"))
    ).expanduser().resolve()

    try:
        structure = _structure_service.extract(pdf_path)
    except Exception as exc:
        return [TextContent(type="text", text=f"ERROR extracting structure: {exc}")]

    if not structure.field_widgets:
        return [TextContent(type="text", text="ERROR: No AcroForm fields found in this PDF.")]

    alias_map = _alias_registry.assign(structure.field_widgets)
    provided_values: dict[str, Any] = {}
    if args.get("data_json") is not None:
        try:
            provided_values = _coerce_json_object(args.get("data_json"), "data_json")
        except ValueError as exc:
            return [TextContent(type="text", text=f"ERROR: {exc}")]

    analyzed_rows: list[dict[str, Any]] = []
    generated_demo_values: dict[str, str] = {}
    for alias, widget in sorted(alias_map.field_widgets.items()):
        nearby = _find_nearby_text(widget.bbox, widget.page, structure.text_blocks)
        label_guess, confidence, _ = _guess_semantics(
            field_name=widget.name,
            nearby_text=nearby,
            field_type=widget.field_type,
        )
        analyzed_rows.append({"alias": alias, "field_name": widget.name, "label_guess": label_guess, "confidence": confidence})
        if mode == "demo" and alias not in provided_values and widget.name not in provided_values:
            generated_demo_values[alias] = _demo_value_for_field(label_guess, widget.field_type)

    fill_values = dict(generated_demo_values)
    fill_values.update(provided_values)

    fill_report = _fill_pdf_document_report(
        pdf_path=pdf_path,
        output_pdf_path=output_pdf_path,
        values=fill_values,
        alias_map=alias_map.alias_to_field,
        enforce_logic=(mode == "demo"),
    )
    fill_log = fill_report["fill_log"]
    unresolved = [k for k, status in fill_log.items() if not status.startswith("ok:")]
    logic_issues = _detect_logic_issues(output_pdf_path)
    repair_summary: dict[str, Any] | None = None
    if auto_fix_logic and logic_issues:
        repair_summary = _auto_fix_logic_issues(output_pdf_path)
        logic_issues = _detect_logic_issues(output_pdf_path)
    try:
        metrics = _basic_fill_metrics(output_pdf_path)
    except ImportError as exc:
        return [TextContent(type="text", text=f"ERROR: PyMuPDF (fitz) is required: {exc}")]
    quality_score = round(
        max(
            0.0,
            min(
                1.0,
                metrics["fill_ratio"] - (0.08 * len(unresolved)) - (0.12 * len(logic_issues)),
            ),
        ),
        3,
    )
    completion_is_complete = (not unresolved) and (not strict_validation or not logic_issues)
    result = {
        "mode": mode,
        "source_pdf": str(pdf_path),
        "output_pdf": str(output_pdf_path),
        "fields_detected": len(alias_map.alias_to_field),
        "values_attempted": len(fill_values),
        "filled_successfully": sum(1 for status in fill_log.values() if status.startswith("ok:")),
        "unresolved_count": len(unresolved),
        "unresolved_fields": unresolved[:50],
        "completion_status": "complete" if completion_is_complete else "partial",
        "demo_values_generated": len(generated_demo_values),
        "alias_map_json": json.dumps(alias_map.alias_to_field),
        "strict_validation": strict_validation,
        "auto_fix_logic": auto_fix_logic,
        "review_recommendation": (
            "No unresolved fields detected."
            if completion_is_complete
            else "Review unresolved_fields/logic_issues and rerun with corrected data_json."
        ),
        "changed_fields": fill_report["changed_fields"][:200],
        "logic_issues_count": len(logic_issues),
        "logic_issues_sample": logic_issues[:25],
        "repair_summary": repair_summary,
        "fill_ratio": metrics["fill_ratio"],
        "quality_score": quality_score,
    }

    content: list[TextContent | ImageContent] = [TextContent(type="text", text=json.dumps(result, indent=2))]
    if preview_pages:
        try:
            for i, img_b64 in enumerate(_render_pages(output_pdf_path), start=1):
                content.append(TextContent(type="text", text=f"--- Filled Form Preview Page {i} ---"))
                content.append(ImageContent(type="image", data=img_b64, mimeType="image/jpeg"))
        except Exception as exc:
            content.append(TextContent(type="text", text=f"WARNING: preview rendering failed: {exc}"))
    return content


async def _fill_form(args: dict[str, Any]) -> list[TextContent]:
    session = get_session(args.get("session_id"))
    try:
        pdf_path = resolve_pdf_source(args, default_path=(session.get("pdf_path") if session else None))
    except ValueError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]
    if not pdf_path.exists():
        return [TextContent(type="text", text=f"ERROR: File not found: {pdf_path}")]
    output_pdf_path = Path(
        str(args.get("output_pdf_path") or (pdf_path.parent / f"{pdf_path.stem}_semantic_filled.pdf"))
    ).expanduser().resolve()
    try:
        semantic_data = _coerce_json_object(args.get("semantic_data_json"), "semantic_data_json")
    except ValueError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]

    try:
        structure = _structure_service.extract(pdf_path)
    except Exception as exc:
        return [TextContent(type="text", text=f"ERROR extracting structure: {exc}")]
    alias_map = _alias_registry.assign(structure.field_widgets)
    mapped_values, mapping_report = _map_semantic_data_to_aliases(
        semantic_data=semantic_data,
        alias_map=alias_map.alias_to_field,
        structure=structure,
    )
    fill_report = _fill_pdf_document_report(
        pdf_path=pdf_path,
        output_pdf_path=output_pdf_path,
        values=mapped_values,
        alias_map=alias_map.alias_to_field,
    )
    fill_log = fill_report["fill_log"]
    return [TextContent(type="text", text=json.dumps({
        "source_pdf": str(pdf_path),
        "output_pdf": str(output_pdf_path),
        "semantic_keys": len(semantic_data),
        "mapped_fields": len(mapped_values),
        "mapping_report": mapping_report,
        "fill_log": fill_log,
        "changed_fields": fill_report["changed_fields"][:200],
    }, indent=2))]


async def _validate_form(args: dict[str, Any]) -> list[TextContent]:
    try:
        pdf_path = resolve_pdf_source(args)
    except ValueError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]
    if not pdf_path.exists():
        return [TextContent(type="text", text=f"ERROR: File not found: {pdf_path}")]
    expected_min_fill_ratio = float(args.get("expected_min_fill_ratio", 0.6))
    expected_min_fill_ratio = max(0.0, min(1.0, expected_min_fill_ratio))

    try:
        total, populated, fill_ratio, likely_empty = _basic_fill_metrics(pdf_path)
    except ImportError as exc:
        return [TextContent(type="text", text=f"ERROR: PyMuPDF (fitz) is required: {exc}")]
    result = {
        "pdf_path": str(pdf_path),
        "total_fields": total,
        "populated_fields": populated,
        "fill_ratio": round(fill_ratio, 3),
        "status": "pass" if fill_ratio >= expected_min_fill_ratio else "warn",
        "expected_min_fill_ratio": expected_min_fill_ratio,
        "likely_empty_fields_sample": likely_empty[:50],
    }
    logic_issues = _detect_logic_issues(pdf_path)
    result["logic_issues_count"] = len(logic_issues)
    result["logic_issues_sample"] = logic_issues[:50]
    if logic_issues and result["status"] == "pass":
        result["status"] = "warn"
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _map_fill_validate(args: dict[str, Any]) -> list[TextContent | ImageContent]:
    completed = await _complete_form(args)
    if not completed:
        return [TextContent(type="text", text="ERROR: complete_form returned no result")]

    first = completed[0]
    if not isinstance(first, TextContent):
        return [TextContent(type="text", text="ERROR: complete_form returned unexpected payload")]

    try:
        complete_payload = json.loads(first.text)
    except json.JSONDecodeError:
        return [TextContent(type="text", text="ERROR: complete_form response was not valid JSON")]

    validate_args = {
        "pdf_path": complete_payload.get("output_pdf"),
        "expected_min_fill_ratio": args.get("expected_min_fill_ratio", 0.6),
    }
    validation = await _validate_form(validate_args)
    if not validation:
        return completed

    validation_text = validation[0].text if isinstance(validation[0], TextContent) else "{}"
    try:
        validation_payload = json.loads(validation_text)
    except json.JSONDecodeError:
        validation_payload = {"raw": validation_text}

    merged = {
        "pipeline": "map_fill_validate",
        "complete_form": complete_payload,
        "validation": validation_payload,
    }
    tail = completed[1:] if len(completed) > 1 else []
    return [TextContent(type="text", text=json.dumps(merged, indent=2)), *tail]


def _coerce_json_object(value: Any, field_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"parsing {field_name} as JSON failed: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ValueError(f"{field_name} must decode to a JSON object")
        return {str(k): v for k, v in decoded.items()}
    raise ValueError(f"{field_name} must be a JSON object or a JSON string")


def _workflow_guide() -> list[TextContent]:
    payload = {
        "purpose": "Agent-friendly workflow for extracting, mapping, and filling AcroForm PDFs.",
        "recommended_tool_order": [
            "one_shot_fill_form (or: complete_form / map_fill_validate)",
        ],
        "tool_aliases": {
            "prepare_form_for_analysis": "extract_form_fields",
            "fill_this_pdf": "fill_pdf_form",
        },
        "templates": {
            "analyze_form": {
                "pdf_path": "/path/to/form.pdf",
                "pdf_bytes_base64": "<optional base64 bytes>",
                "annotate_pages": True,
                "ambiguity_threshold": 0.72,
                "persist_session": True,
            },
            "extract_form_fields": {
                "pdf_path": "/path/to/form.pdf",
                "pdf_bytes_base64": "<optional base64 bytes>",
                "annotate_pages": True,
            },
            "save_field_mapping": {
                "session_id": "<preferred>",
                "pdf_path": "/path/to/form.pdf",
                "alias_map_json": "{\"F001\":\"field.name\"}",
                "field_analysis_json": "{\"F001\":{\"label\":\"...\",\"context\":\"...\",\"expected_value_type\":\"string\",\"expected_format\":null,\"is_required\":false,\"section\":null}}",
                "form_family": "my_form",
                "version": "1",
            },
            "fill_pdf_form": {
                "session_id": "<preferred>",
                "pdf_path": "/path/to/form.pdf",
                "pdf_bytes_base64": "<optional base64 bytes>",
                "values_json": "{\"F001\":\"Alice Example\"}",
                "alias_map_json": "{\"F001\":\"field.name\"}",
                "output_pdf_path": "/optional/path/filled.pdf",
            },
            "complete_form": {
                "session_id": "<optional>",
                "pdf_path": "/path/to/form.pdf",
                "pdf_bytes_base64": "<optional base64 bytes>",
                "mode": "demo",
                "preview_pages": True,
                "strict_validation": True,
                "auto_fix_logic": True,
            },
            "one_shot_fill_form": {
                "pdf_path": "/path/to/form.pdf",
                "mode": "demo",
                "preview_pages": True,
            },
            "fill_form": {
                "session_id": "<optional>",
                "pdf_path": "/path/to/form.pdf",
                "pdf_bytes_base64": "<optional base64 bytes>",
                "semantic_data_json": {
                    "full_name": "Jordan Demo",
                    "date": "03/26/2026",
                    "filing_status": "single",
                },
            },
            "validate_form": {
                "pdf_path": "/path/to/filled.pdf",
                "pdf_bytes_base64": "<optional base64 bytes>",
                "expected_min_fill_ratio": 0.6,
            },
            "map_fill_validate": {
                "pdf_path": "/path/to/form.pdf",
                "mode": "demo",
                "preview_pages": True,
                "expected_min_fill_ratio": 0.6,
            },
        },
        "notes": [
            "All *_json inputs accept either JSON strings or native objects.",
            "If proxied mount path rewriting fails, pass pdf_bytes_base64 instead of pdf_path.",
            "Use session_id to avoid brittle handoffs when tools are called across separate turns.",
            "Use alias_map_json when values_json keys are FXXX aliases.",
            "For best quality, use analyze_form and only manually review ambiguous_fields.",
            "Use one_shot_fill_form for the fastest end-to-end path (analyze + fill + validate + auto-fix).",
            "Use complete_form for a configurable single-call pipeline with strict_validation and auto_fix_logic.",
            "Use fill_form if you only have semantic keys and want auto-mapping.",
            "annotate_pages=true returns annotated images directly; no separate local annotation script is required.",
        ],
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _guess_semantics(field_name: str, nearby_text: str, field_type: str) -> tuple[str, float, str]:
    candidate = (nearby_text or field_name or "").strip()
    if not candidate:
        return ("unknown_field", 0.25, "No nearby text or useful field name found.")

    normalized = " ".join(candidate.split())
    confidence = 0.55
    rationale = "Derived from nearby printed text."
    if nearby_text:
        confidence += 0.2
    if len(normalized) <= 60:
        confidence += 0.1
    if field_type.lower() in {"btn", "button", "checkbox"}:
        rationale = "Likely checkbox/button based on field type and nearby text."
    if any(token in normalized.lower() for token in ("date", "ssn", "zip", "phone", "email", "income", "expense")):
        confidence += 0.1
        rationale = "Nearby text includes recognizable semantic keywords."

    confidence = max(0.05, min(0.99, confidence))
    label = normalized[:120]
    return (label, confidence, rationale)


def _section_hint(
    page: int,
    bbox: tuple[float, float, float, float],
    text_blocks: list[TextBlock],
) -> str | None:
    x0, y0, x1, y1 = bbox
    center_y = (y0 + y1) / 2
    candidates = []
    for block in text_blocks:
        if block.page != page:
            continue
        bx0, by0, bx1, by1 = block.bbox
        if by1 > center_y:
            continue
        text = (block.text or "").strip()
        if not text:
            continue
        vertical_gap = center_y - by1
        if vertical_gap > 120:
            continue
        width = bx1 - bx0
        if width < 80:
            continue
        candidates.append((vertical_gap, text))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1][:80]


def _fill_pdf_document(
    pdf_path: Path,
    output_pdf_path: Path,
    values: dict[str, Any],
    alias_map: dict[str, str],
) -> dict[str, str]:
    return _fill_pdf_document_report(
        pdf_path=pdf_path,
        output_pdf_path=output_pdf_path,
        values=values,
        alias_map=alias_map,
    )["fill_log"]


def _fill_pdf_document_report(
    pdf_path: Path,
    output_pdf_path: Path,
    values: dict[str, Any],
    alias_map: dict[str, str],
    *,
    enforce_logic: bool = False,
) -> dict[str, Any]:
    import fitz

    fill_log: dict[str, str] = {}
    changed_fields: list[dict[str, str]] = []
    with fitz.open(str(pdf_path)) as doc:
        widgets_by_name: dict[str, list[Any]] = {}
        for page in doc:
            for widget in page.widgets() or []:
                if widget.field_name:
                    widgets_by_name.setdefault(str(widget.field_name), []).append(widget)

        resolved_values: list[tuple[str, str, Any]] = []
        field_values_by_name: dict[str, Any] = {}
        for key, raw_value in values.items():
            key_str = str(key)
            field_name = alias_map.get(key_str, key_str)
            resolved_values.append((key_str, field_name, raw_value))
            field_values_by_name[field_name] = raw_value

        if enforce_logic:
            _apply_conditional_field_rules(field_values_by_name, widgets_by_name)
            normalized: list[tuple[str, str, Any]] = []
            for key_str, field_name, raw_value in resolved_values:
                normalized.append((key_str, field_name, field_values_by_name.get(field_name, raw_value)))
            resolved_values = normalized

        for key_str, field_name, raw_value in resolved_values:
            widgets = widgets_by_name.get(field_name) or []
            if not widgets:
                fill_log[key_str] = f"missing_field:{field_name}"
                continue

            if len(widgets) > 1:
                status = _set_widget_group_value(
                    widgets=widgets,
                    raw_value=raw_value,
                    input_key=key_str,
                    field_name=field_name,
                    changed_fields=changed_fields,
                )
                fill_log[key_str] = status
                continue

            widget = widgets[0]
            if isinstance(raw_value, bool) and _is_checkbox_widget(widget):
                value = _checkbox_target_value(widget, raw_value)
            else:
                value = "" if raw_value is None else str(raw_value)
            try:
                before = str(widget.field_value or "")
                widget.field_value = value
                widget.update()
                after = str(widget.field_value or "")
                fill_log[key_str] = f"ok:{field_name}"
                if before != after:
                    changed_fields.append(
                        {
                            "input_key": key_str,
                            "field_name": field_name,
                            "before": before,
                            "after": after,
                        }
                    )
            except Exception as exc:
                fill_log[key_str] = f"error:{field_name}:{exc}"

        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_pdf_path))
    return {
        "fill_log": fill_log,
        "changed_fields": changed_fields,
    }


def _is_checkbox_widget(widget: Any) -> bool:
    fts = str(getattr(widget, "field_type_string", "")).lower()
    return "checkbox" in fts or "button" in fts or str(getattr(widget, "field_type", "")) == "2"


def _checkbox_target_value(widget: Any, selected: bool) -> str:
    if not selected:
        return "Off"
    try:
        if hasattr(widget, "on_state"):
            return str(widget.on_state())
    except Exception:
        pass
    try:
        states = widget.button_states() or {}
        normal = states.get("normal") or []
        for state in normal:
            state_s = str(state)
            if state_s.lower() != "off":
                return state_s
    except Exception:
        pass
    return "Yes"


def _set_widget_group_value(
    widgets: list[Any],
    raw_value: Any,
    input_key: str,
    field_name: str,
    changed_fields: list[dict[str, str]],
) -> str:
    try:
        normalized = str(raw_value).strip().lower()
    except Exception:
        normalized = ""

    target_idx = 0
    if isinstance(raw_value, bool):
        if raw_value:
            target_idx = _pick_yes_widget_index(widgets)
        else:
            target_idx = -1
    elif normalized in {"yes", "true", "on", "1"}:
        target_idx = _pick_yes_widget_index(widgets)
    elif normalized in {"no", "false", "off", "0"}:
        target_idx = _pick_no_widget_index(widgets)
    else:
        explicit = _pick_widget_matching_state(widgets, str(raw_value))
        if explicit is not None:
            target_idx = explicit

    try:
        for idx, widget in enumerate(widgets):
            before = str(widget.field_value or "")
            if target_idx == -1:
                after_value = "Off"
            elif idx == target_idx:
                after_value = _checkbox_target_value(widget, True)
            else:
                after_value = "Off"
            widget.field_value = after_value
            widget.update()
            after = str(widget.field_value or "")
            if before != after:
                changed_fields.append(
                    {
                        "input_key": input_key,
                        "field_name": f"{field_name}[{idx}]",
                        "before": before,
                        "after": after,
                    }
                )
    except Exception as exc:
        return f"error:{field_name}:{exc}"
    return f"ok:{field_name}"


def _pick_widget_matching_state(widgets: list[Any], raw: str) -> int | None:
    target = raw.strip().lower()
    for idx, widget in enumerate(widgets):
        state = _checkbox_target_value(widget, True).lower()
        if state == target:
            return idx
    return None


def _pick_yes_widget_index(widgets: list[Any]) -> int:
    for idx, widget in enumerate(widgets):
        name = str(getattr(widget, "field_name", "")).lower()
        on_state = _checkbox_target_value(widget, True).lower()
        if "yes" in on_state or on_state in {"1", "on", "true"} or "yes" in name:
            return idx
    return 0


def _pick_no_widget_index(widgets: list[Any]) -> int:
    for idx, widget in enumerate(widgets):
        name = str(getattr(widget, "field_name", "")).lower()
        on_state = _checkbox_target_value(widget, True).lower()
        if "no" in on_state or on_state in {"0", "off", "false"} or "no" in name:
            return idx
    return -1


def _normalize_checkbox_choice(raw_value: Any) -> str | None:
    if isinstance(raw_value, bool):
        return "yes" if raw_value else "no"
    text = str(raw_value or "").strip().lower()
    if text in {"yes", "true", "on", "1"}:
        return "yes"
    if text in {"no", "false", "off", "0"}:
        return "no"
    return None


def _group_selected_yes_no(widgets: list[Any], raw_value: Any) -> str | None:
    choice = _normalize_checkbox_choice(raw_value)
    if choice is not None:
        return choice
    explicit = str(raw_value or "").strip().lower()
    if explicit:
        for widget in widgets:
            if _checkbox_target_value(widget, True).strip().lower() == explicit:
                return "yes"
    states = {
        _checkbox_target_value(widget, True).strip().lower()
        for widget in widgets
    }
    has_yes = "yes" in states
    has_no = "no" in states
    if has_yes and has_no:
        return "yes"
    return None


def _apply_conditional_field_rules(
    field_values_by_name: dict[str, Any],
    widgets_by_name: dict[str, list[Any]],
) -> None:
    for field_name, widgets in widgets_by_name.items():
        if len(widgets) < 2:
            continue
        if field_name not in field_values_by_name:
            continue
        selected = _group_selected_yes_no(widgets, field_values_by_name[field_name])
        if selected != "no":
            continue

        dependent_match = re.fullmatch(r"check2([a-e])", field_name.strip(), flags=re.IGNORECASE)
        if dependent_match:
            suffix = dependent_match.group(1).lower()
            field_values_by_name[f"Dependant Relation 2{suffix}"] = ""
            field_values_by_name[f"Dependant age 2{suffix}"] = ""
            continue

        other_match = re.fullmatch(r"check(\d+)", field_name.strip(), flags=re.IGNORECASE)
        if other_match:
            line_num = other_match.group(1)
            other_field = f"Other {line_num}"
            if other_field in widgets_by_name or other_field in field_values_by_name:
                field_values_by_name[other_field] = ""


def _detect_logic_issues(pdf_path: Path) -> list[dict[str, Any]]:
    import fitz

    issues: list[dict[str, Any]] = []
    with fitz.open(str(pdf_path)) as doc:
        widgets_by_name: dict[str, list[Any]] = {}
        text_values: dict[str, str] = {}
        for page in doc:
            for widget in page.widgets() or []:
                name = str(widget.field_name or "")
                if not name:
                    continue
                widgets_by_name.setdefault(name, []).append(widget)
                if not _is_checkbox_widget(widget):
                    text_values[name] = str(widget.field_value or "").strip()

        for name, widgets in widgets_by_name.items():
            if len(widgets) < 2:
                continue
            states = {_checkbox_target_value(w, True).strip().lower() for w in widgets}
            if not ({"yes", "no"} & states):
                continue
            selected = _selected_yes_no_from_widgets(widgets)
            if selected != "no":
                continue

            dep = re.fullmatch(r"check2([a-e])", name.strip(), flags=re.IGNORECASE)
            if dep:
                suffix = dep.group(1).lower()
                for related in (f"Dependant Relation 2{suffix}", f"Dependant age 2{suffix}"):
                    if text_values.get(related):
                        issues.append({
                            "type": "conditional_text_filled_when_no",
                            "checkbox_group": name,
                            "related_field": related,
                            "value": text_values.get(related),
                        })
                continue

            other = re.fullmatch(r"check(\d+)", name.strip(), flags=re.IGNORECASE)
            if other:
                related = f"Other {other.group(1)}"
                if text_values.get(related):
                    issues.append({
                        "type": "conditional_text_filled_when_no",
                        "checkbox_group": name,
                        "related_field": related,
                        "value": text_values.get(related),
                    })

    return issues


def _selected_yes_no_from_widgets(widgets: list[Any]) -> str | None:
    for widget in widgets:
        current = str(getattr(widget, "field_value", "") or "").strip().lower()
        if not current or current == "off":
            continue
        on_state = _checkbox_target_value(widget, True).strip().lower()
        if current == on_state:
            if "no" in on_state:
                return "no"
            if "yes" in on_state:
                return "yes"
            return "yes"
    return None


def _auto_fix_logic_issues(pdf_path: Path) -> dict[str, Any]:
    import fitz

    cleared: list[str] = []
    with fitz.open(str(pdf_path)) as doc:
        widgets_by_name: dict[str, list[Any]] = {}
        for page in doc:
            for widget in page.widgets() or []:
                name = str(widget.field_name or "")
                if not name:
                    continue
                widgets_by_name.setdefault(name, []).append(widget)

        for name, widgets in widgets_by_name.items():
            if len(widgets) < 2:
                continue
            selected = _selected_yes_no_from_widgets(widgets)
            if selected != "no":
                continue

            dep = re.fullmatch(r"check2([a-e])", name.strip(), flags=re.IGNORECASE)
            if dep:
                suffix = dep.group(1).lower()
                for related in (f"Dependant Relation 2{suffix}", f"Dependant age 2{suffix}"):
                    for widget in widgets_by_name.get(related, []):
                        before = str(widget.field_value or "").strip()
                        if before:
                            widget.field_value = ""
                            widget.update()
                            cleared.append(related)
                continue

            other = re.fullmatch(r"check(\d+)", name.strip(), flags=re.IGNORECASE)
            if other:
                related = f"Other {other.group(1)}"
                for widget in widgets_by_name.get(related, []):
                    before = str(widget.field_value or "").strip()
                    if before:
                        widget.field_value = ""
                        widget.update()
                        cleared.append(related)

        if cleared:
            doc.save(str(pdf_path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)

    return {
        "fields_cleared_count": len(cleared),
        "fields_cleared_sample": sorted(set(cleared))[:25],
    }


def _basic_fill_metrics(pdf_path: Path) -> tuple[int, int, float, list[dict[str, Any]]]:
    import fitz

    total = 0
    populated = 0
    likely_empty: list[dict[str, Any]] = []
    with fitz.open(str(pdf_path)) as doc:
        for page_index, page in enumerate(doc, start=1):
            for widget in page.widgets() or []:
                if not widget.field_name:
                    continue
                total += 1
                value = str(widget.field_value or "").strip()
                if value:
                    populated += 1
                else:
                    likely_empty.append({"field_name": str(widget.field_name), "page": page_index})
    fill_ratio = (populated / total) if total else 0.0
    return total, populated, fill_ratio, likely_empty


def _demo_value_for_field(label_guess: str, field_type: str) -> str:
    text = (label_guess or "").lower()
    if field_type.lower() in {"btn", "checkbox", "button"}:
        if any(tok in text for tok in ("dependents", "with you", "joint case", "increase", "decrease", "amended")):
            return "Yes"
        if any(tok in text for tok in ("separate household", "supplement", "other expenses include")):
            return "No"
        return "No"
    if "date" in text:
        return "01/15/2026"
    if "case" in text and "number" in text:
        return "26-10042"
    if "zip" in text:
        return "60601"
    if "phone" in text:
        return "(312) 555-0198"
    if "email" in text:
        return "demo.filer@example.com"
    if "name" in text:
        return "Jordan Avery Demo"
    if any(tok in text for tok in ("income", "expense", "amount", "total", "rent", "tax", "insurance")):
        return "450.00"
    return "Demo Value"


def _map_semantic_data_to_aliases(
    semantic_data: dict[str, Any],
    alias_map: dict[str, str],
    structure: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    label_index: dict[str, tuple[str, str, float]] = {}
    widget_by_name = {w.name: w for w in structure.field_widgets}
    for alias, field_name in alias_map.items():
        widget = widget_by_name.get(field_name)
        if widget is None:
            continue
        nearby = _find_nearby_text(widget.bbox, widget.page, structure.text_blocks)
        label, confidence, _ = _guess_semantics(field_name, nearby, widget.field_type)
        label_index[alias] = (field_name, label.lower(), confidence)

    mapped: dict[str, Any] = {}
    report: list[dict[str, Any]] = []
    for semantic_key, value in semantic_data.items():
        key_tokens = set(_tokenize(str(semantic_key)))
        best_alias = None
        best_score = -1.0
        for alias, (_field_name, label_lower, base_conf) in label_index.items():
            label_tokens = set(_tokenize(label_lower))
            if not label_tokens:
                continue
            overlap = len(key_tokens & label_tokens)
            score = overlap / max(len(key_tokens), 1)
            score = score * 0.8 + base_conf * 0.2
            if score > best_score:
                best_score = score
                best_alias = alias
        if best_alias is not None and best_score >= 0.35:
            mapped[best_alias] = value
            report.append({"semantic_key": semantic_key, "mapped_to": best_alias, "score": round(best_score, 3)})
        else:
            report.append({"semantic_key": semantic_key, "mapped_to": None, "score": round(max(best_score, 0.0), 3)})
    return mapped, report


def _tokenize(text: str) -> list[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return [tok for tok in cleaned.split() if tok]


# ---------------------------------------------------------------------------
# Page rendering helper
# ---------------------------------------------------------------------------

def _render_pages(pdf_path: Path, dpi: int = 96) -> list[str]:
    """Render each page of *pdf_path* to a base64-encoded JPEG string."""
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF (fitz) is required. Install with: pip install pymupdf") from exc

    images: list[str] = []
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    with fitz.open(str(pdf_path)) as doc:
        for page_index in range(doc.page_count):
            pix = doc.load_page(page_index).get_pixmap(matrix=mat, alpha=False)
            images.append(base64.standard_b64encode(pix.tobytes("jpg", jpg_quality=80)).decode())
    return images


def _find_nearby_text(
    bbox: tuple[float, float, float, float],
    page: int,
    text_blocks: list[TextBlock],
    max_dist: float = 60.0,
) -> str:
    fx0, fy0, fx1, fy1 = bbox
    f_cx = (fx0 + fx1) / 2
    f_cy = (fy0 + fy1) / 2
    best_text = ""
    best_score = float("inf")
    for block in text_blocks:
        if block.page != page:
            continue
        text = (block.text or "").strip()
        if not text:
            continue
        bx0, by0, bx1, by1 = block.bbox
        b_cx = (bx0 + bx1) / 2
        b_cy = (by0 + by1) / 2
        dist = math.hypot(f_cx - b_cx, f_cy - b_cy)
        if dist > max_dist:
            continue
        vert_offset = abs(f_cy - b_cy)
        left_bonus = 0.0 if bx1 <= fx0 + 5 else 20.0
        score = dist + vert_offset * 0.5 + left_bonus
        if score < best_score:
            best_score = score
            best_text = text
    if len(best_text) > 120:
        best_text = best_text[:120].rsplit(" ", 1)[0] + "…"
    return best_text


def _position_hint(
    bbox: tuple[float, float, float, float],
    page: int,
    page_dim: Any,
) -> str:
    if page_dim is None:
        return f"page {page + 1}"
    cx = (bbox[0] + bbox[2]) / 2 / page_dim.width
    cy = (bbox[1] + bbox[3]) / 2 / page_dim.height
    horiz = "left" if cx < 0.4 else ("right" if cx > 0.6 else "center")
    vert = "upper" if cy < 0.33 else ("lower" if cy > 0.66 else "middle")
    return f"page {page + 1}, {vert}-{horiz}"


# ---------------------------------------------------------------------------
# Entry points — stdio and HTTP/SSE
# ---------------------------------------------------------------------------

async def main() -> None:
    """Run over stdio (default for local Claude Code MCP config)."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


async def main_http(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run over HTTP with SSE transport.

    Claude Code connects via:  ``{ "url": "http://<host>:<port>/sse" }``
    """
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.routing import Mount, Route

    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0],
                streams[1],
                server.create_initialization_options(),
            )

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )

    print(f"fillform MCP server listening on http://{host}:{port}/sse")
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()


def _cli() -> None:
    """CLI entry point: parses --http / --host / --port flags."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="fillform MCP server")
    parser.add_argument(
        "--http", action="store_true",
        help="Run HTTP/SSE server instead of stdio",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (HTTP mode, default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (HTTP mode, default 8000)")
    args = parser.parse_args()

    if args.http:
        asyncio.run(main_http(host=args.host, port=args.port))
    else:
        asyncio.run(main())


if __name__ == "__main__":
    _cli()
