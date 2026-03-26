"""Real MCP server for fillform.

This server exposes the form-analysis pipeline as MCP tools so that a running
Claude Code session can analyse PDF forms **without making any additional API
calls**.  Claude receives annotated page images directly as tool-response content
and performs the vision analysis in its own context.

Tools
-----
fillform_workflow_guide
    Returns a compact quickstart and tool-call templates for agents.

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
                "required": ["alias_map_json", "field_analysis_json"],
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
                    "pdf_path": {
                        "type": "string",
                        "description": "Absolute or relative path to source PDF.",
                    },
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
                    "output_pdf_path": {
                        "type": "string",
                        "description": (
                            "Optional output path. Defaults to <input_stem>_filled.pdf "
                            "next to the source PDF."
                        ),
                    },
                },
                "required": ["pdf_path", "values_json"],
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
    if name == "extract_form_fields":
        return await _extract_fields(arguments)
    if name == "prepare_form_for_analysis":
        return await _extract_fields(arguments)
    if name == "save_field_mapping":
        return await _save_mapping(arguments)
    if name == "fill_pdf_form":
        return await _fill_pdf_form(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _extract_fields(args: dict[str, Any]) -> list[TextContent | ImageContent]:
    pdf_path = Path(args["pdf_path"]).expanduser().resolve()
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
                "alias_map_json": "<paste alias_map_json>",
                "field_analysis_json": "{\"F001\": {\"label\": \"...\", \"context\": \"...\", \"expected_value_type\": \"string\", \"expected_format\": null, \"is_required\": false, \"section\": null}}",
                "form_family": "unknown",
                "version": "1",
            },
            "fill_pdf_form": {
                "pdf_path": str(pdf_path),
                "values_json": "{\"F001\": \"value\"}",
                "alias_map_json": "<paste alias_map_json when using FXXX keys>",
            },
        },
    }

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


async def _save_mapping(args: dict[str, Any]) -> list[TextContent]:
    pdf_path_str = args.get("pdf_path") or ""
    form_family = str(args.get("form_family") or "unknown")
    version = str(args.get("version") or "1")

    if pdf_path_str:
        pdf_path = Path(pdf_path_str).expanduser().resolve()
        output_dir = Path(args["output_dir"]).expanduser().resolve() if args.get("output_dir") else pdf_path.parent
    else:
        output_dir = Path(args.get("output_dir") or "/tmp").expanduser().resolve()

    # Parse alias map
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
    pdf_path = Path(str(args.get("pdf_path") or "")).expanduser().resolve()
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

    try:
        import fitz
    except ImportError as exc:
        return [TextContent(type="text", text=f"ERROR: PyMuPDF (fitz) is required: {exc}")]

    fill_log: dict[str, str] = {}
    with fitz.open(str(pdf_path)) as doc:
        widgets_by_name: dict[str, Any] = {}
        for page in doc:
            for widget in page.widgets() or []:
                if widget.field_name:
                    widgets_by_name[str(widget.field_name)] = widget

        for key, raw_value in values.items():
            key_str = str(key)
            field_name = alias_map.get(key_str, key_str)
            widget = widgets_by_name.get(field_name)
            if widget is None:
                fill_log[key_str] = f"missing_field:{field_name}"
                continue

            value = "" if raw_value is None else str(raw_value)
            try:
                widget.field_value = value
                widget.update()
                fill_log[key_str] = f"ok:{field_name}"
            except Exception as exc:
                fill_log[key_str] = f"error:{field_name}:{exc}"

        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_pdf_path))

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
                },
                indent=2,
            ),
        )
    ]


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
            "extract_form_fields",
            "save_field_mapping",
            "fill_pdf_form",
        ],
        "tool_aliases": {
            "prepare_form_for_analysis": "extract_form_fields",
        },
        "templates": {
            "extract_form_fields": {
                "pdf_path": "/path/to/form.pdf",
                "annotate_pages": True,
            },
            "save_field_mapping": {
                "pdf_path": "/path/to/form.pdf",
                "alias_map_json": "{\"F001\":\"field.name\"}",
                "field_analysis_json": "{\"F001\":{\"label\":\"...\",\"context\":\"...\",\"expected_value_type\":\"string\",\"expected_format\":null,\"is_required\":false,\"section\":null}}",
                "form_family": "my_form",
                "version": "1",
            },
            "fill_pdf_form": {
                "pdf_path": "/path/to/form.pdf",
                "values_json": "{\"F001\":\"Alice Example\"}",
                "alias_map_json": "{\"F001\":\"field.name\"}",
                "output_pdf_path": "/optional/path/filled.pdf",
            },
        },
        "notes": [
            "All *_json inputs accept either JSON strings or native objects.",
            "Use alias_map_json when values_json keys are FXXX aliases.",
        ],
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


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
