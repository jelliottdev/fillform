"""Vercel-compatible MCP server for fillform.

Design principles (context-budget aware):
- ``extract_form_fields`` returns **JSON only** by default — no images.
  The alias map + per-field nearby-text labels give Claude everything it
  needs to identify fields from the original PDF attachment already in
  its context.  No server-side image rendering, no huge response.
- Input: ``file_path`` (preferred, ~50 chars) or ``pdf_base64`` (fallback,
  large but unavoidable for remote deployments).
- ``annotate_pages=true`` is an escape hatch that re-enables JPEG page
  images for short PDFs when text labels alone are insufficient.

Claude Code / claude.ai URL config::

    {
      "mcpServers": {
        "fillform": {
          "url": "https://<your-project>.vercel.app"
        }
      }
    }
"""

from __future__ import annotations

import base64
import concurrent.futures
import html
import json
import math
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent / "src"))

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import ImageContent, TextContent, Tool

from fillform.annotator import PdfAnnotator
from fillform.bankruptcy_forms import BANKRUPTCY_INDEX_URL, USCourtsBankruptcyFormsSync
from fillform.bankruptcy_tool import BankruptcyFormsTool, BankruptcySyncRequest
from fillform.contracts import CanonicalField, CanonicalSchema
from fillform.field_alias import FieldAliasRegistry
from fillform.structure import PdfStructureService, PyMuPdfStructureAdapter, TextBlock

# ---------------------------------------------------------------------------
# Server and service singletons
# ---------------------------------------------------------------------------

server = Server("fillform")

_structure_service = PdfStructureService(adapter=PyMuPdfStructureAdapter())
_alias_registry = FieldAliasRegistry()
_annotator = PdfAnnotator()  # only used when annotate_pages=True
_analytics_cache: dict[str, Any] = {"ts": 0, "payload": None}
_ANALYTICS_TTL_SECONDS = 300


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="extract_form_fields",
            description=(
                "Full form-mapping pipeline: assign sequential FXXX aliases (F001, F002, …) "
                "to AcroForm fields, then return everything needed to visually identify each "
                "field via the orange-overlay approach — with ZERO context overflow.\n\n"
                "CRITICAL: You MUST complete ALL 4 steps in order. Do NOT skip steps 3 or 4\n"
                "even if field names look descriptive — field names are internal identifiers\n"
                "and do not reliably describe what the field collects. Visual confirmation\n"
                "via the annotated images is REQUIRED before calling save_field_mapping.\n\n"
                "WORKFLOW (all 4 steps are mandatory):\n"
                "  STEP 1 — extract field data locally (run this python snippet, capture output):\n"
                "    python3 -c \"\n"
                "import json,sys\n"
                "try:\n"
                "    import fitz\n"
                "except ImportError:\n"
                "    sys.exit('PyMuPDF required: pip install pymupdf -- pypdf silently misses checkboxes')\n"
                "doc=fitz.open('YOUR_FILE.pdf'); out=[]\n"
                "for i,p in enumerate(doc):\n"
                "    for w in (p.widgets() or []):\n"
                "        f={'name':w.field_name,'type':w.field_type_string,'page':i,'bbox':list(w.rect),'value':w.field_value}\n"
                "        if w.field_type_string=='CheckBox': f['on_state']=w.on_state()\n"
                "        if w.field_type_string in ('ComboBox','ListBox'): f['choices']=w.choice_values or []\n"
                "        out.append(f)\n"
                "sys.stdout.write(json.dumps(out))\n"
                "    \"\n"
                "  STEP 2 — call this tool with fields_json=<output from step 1>\n"
                "  STEP 3 (MANDATORY) — run the 'annotation_script' from the response.\n"
                "    It overlays orange FXXX labels on the PDF and saves one JPEG per page\n"
                "    to /tmp/fillform_page_N.jpg. You must run this even if field names\n"
                "    appear descriptive. Do not proceed to step 4 until it succeeds.\n"
                "  STEP 4 (MANDATORY) — read EVERY /tmp/fillform_page_N.jpg image and\n"
                "    visually identify what each FXXX label collects from the form layout.\n"
                "    Then call save_field_mapping with your findings.\n\n"
                "INPUT MODES (use fields_json — it sends ~5KB, not the whole PDF):\n"
                "  fields_json  BEST — pre-extracted locally, zero context cost\n"
                "  file_path    local server only (Vercel cannot access local paths)\n"
                "  pdf_base64   AVOID — causes context overflow for PDFs > 200KB"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "fields_json": {
                        "type": "string",
                        "description": (
                            "PREFERRED. JSON array of field objects extracted locally. "
                            "Each object: {\"name\": str, \"type\": str, \"page\": int, "
                            "\"bbox\": [x0, y0, x1, y1]}. "
                            "Run the python snippet in the tool description to produce this."
                        ),
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to the PDF. Only works if the server can access "
                            "the path (local server). On Vercel (remote) this will fail."
                        ),
                    },
                    "pdf_base64": {
                        "type": "string",
                        "description": (
                            "Base64-encoded PDF bytes. Last resort — causes context overflow "
                            "for PDFs over ~200KB. Do NOT produce this via bash."
                        ),
                    },
                    "annotate_pages": {
                        "type": "boolean",
                        "description": (
                            "Default false. Set true to also receive annotated JPEG page "
                            "images alongside the JSON. Avoid for PDFs > 3 pages."
                        ),
                        "default": False,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="save_field_mapping",
            description=(
                "Build a CanonicalSchema from Claude's field analysis and return the "
                "schema JSON plus a plain-text AI fill guide (fill script). "
                "Call this after you have identified what every FXXX field collects "
                "by visually inspecting the annotated JPEG images."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "alias_map_json": {
                        "type": "string",
                        "description": "The alias_map JSON string from extract_form_fields.",
                    },
                    "field_analysis_json": {
                        "type": "string",
                        "description": (
                            'JSON mapping each alias to its semantic description. '
                            'Structure: {"F001": {"label":"…","context":"…",'
                            '"expected_value_type":"string|date|number|boolean|signature|selection",'
                            '"expected_format":"…or null","is_required":true|false,"section":"…",'
                            '"on_state":"yes"|null,"choices":["…"]|null}, …}'
                        ),
                    },
                    "form_family": {
                        "type": "string",
                        "description": "Logical form family name (e.g. 'W-9', 'intake_form').",
                        "default": "unknown",
                    },
                    "version": {
                        "type": "string",
                        "description": "Schema version string.",
                        "default": "1",
                    },
                },
                "required": ["alias_map_json", "field_analysis_json"],
            },
        ),
        Tool(
            name="validate_fill",
            description=(
                "Validate a filled PDF against the agent's intended values. "
                "Call this after filling the form to catch errors before presenting to the user.\n\n"
                "WORKFLOW:\n"
                "  1. After filling the PDF, run the STEP 1 extraction snippet on the FILLED PDF "
                "     to get actual field values (the snippet captures 'value' for each field).\n"
                "  2. Call this tool with: filled_fields_json (output from step 1), "
                "     intended_values (alias→value dict you tried to set), "
                "     alias_map_json (from extract_form_fields), "
                "     and optionally schema_json (from save_field_mapping for rule checking).\n"
                "  3. Read the ValidationReport. If passed=false, fix flagged fields and re-validate.\n"
                "  4. Max 3 refinement iterations, then surface remaining issues to user."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filled_fields_json": {
                        "type": "string",
                        "description": (
                            "JSON array from running the STEP 1 snippet on the FILLED PDF. "
                            "Each object must include 'name', 'type', 'value', and optionally "
                            "'on_state' (CheckBox) and 'choices' (ComboBox/ListBox)."
                        ),
                    },
                    "intended_values": {
                        "type": "string",
                        "description": (
                            'JSON object mapping alias → value the agent intended to set. '
                            'E.g. {"F001": "John Smith", "F005": "yes", "F010": "Off"}'
                        ),
                    },
                    "alias_map_json": {
                        "type": "string",
                        "description": "The alias_map JSON from extract_form_fields (alias→field_name).",
                    },
                    "schema_json": {
                        "type": "string",
                        "description": (
                            "Optional. Schema JSON from save_field_mapping. "
                            "Enables on_state checks, choices validation, and required-field checks."
                        ),
                    },
                },
                "required": ["filled_fields_json", "intended_values", "alias_map_json"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent | ImageContent]:
    if name in ("extract_form_fields", "prepare_form_for_analysis"):  # backward compat
        return await _extract_fields(arguments)
    if name == "save_field_mapping":
        return await _save_mapping(arguments)
    if name == "validate_fill":
        return await _validate_fill(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# extract_form_fields implementation
# ---------------------------------------------------------------------------

async def _extract_fields(args: dict[str, Any]) -> list[TextContent | ImageContent]:
    fields_json_str = args.get("fields_json") or ""
    file_path = args.get("file_path") or ""
    pdf_b64 = args.get("pdf_base64") or ""
    annotate_pages = bool(args.get("annotate_pages", False))

    if not fields_json_str and not file_path and not pdf_b64:
        return [TextContent(type="text", text=(
            "ERROR: provide fields_json, file_path, or pdf_base64.\n"
            "BEST OPTION: run the python snippet from the tool description locally "
            "to extract field data, then pass the output as fields_json. "
            "This sends only ~5KB instead of the whole PDF."
        ))]

    # -----------------------------------------------------------------------
    # MODE 1: pre-extracted fields_json — no PDF needed on server
    # -----------------------------------------------------------------------
    if fields_json_str:
        try:
            raw_fields: list[dict[str, Any]] = json.loads(fields_json_str)
        except json.JSONDecodeError as exc:
            return [TextContent(type="text", text=f"ERROR parsing fields_json: {exc}")]

        if not raw_fields:
            return [TextContent(type="text", text=(
                "No fields found in fields_json. "
                "Make sure the PDF has interactive AcroForm fields and the "
                "extraction script ran correctly."
            ))]

        # Build FieldWidget-like objects to feed the alias registry
        from fillform.structure import FieldWidget
        widgets = []
        for f in raw_fields:
            bbox = f.get("bbox") or [0, 0, 100, 20]
            widgets.append(FieldWidget(
                name=str(f.get("name") or "unknown"),
                field_type=str(f.get("type") or "Tx"),
                page=int(f.get("page") or 0),
                bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
            ))

        alias_map = _alias_registry.assign(widgets)

        # Build a lookup from field name → raw extraction data for on_state/choices passthrough
        raw_by_name: dict[str, dict[str, Any]] = {}
        for f in raw_fields:
            raw_by_name[str(f.get("name") or "")] = f

        fields_data: list[dict[str, Any]] = []
        for alias, widget in alias_map.field_widgets.items():
            raw = raw_by_name.get(widget.name, {})
            entry: dict[str, Any] = {
                "alias": alias,
                "type": widget.field_type,
                "page": widget.page + 1,
                "bbox": [round(v, 1) for v in widget.bbox],
            }
            if "on_state" in raw:
                entry["on_state"] = raw["on_state"]
            if "choices" in raw:
                entry["choices"] = raw["choices"]
            fields_data.append(entry)
        fields_data.sort(key=lambda f: f["alias"])

        annotation_script = _build_annotation_script(fields_data)

        result: dict[str, Any] = {
            "field_count": len(fields_data),
            "alias_map": alias_map.alias_to_field,
            "IMPORTANT": (
                "Field names in alias_map are internal PDF identifiers only — do NOT use them "
                "to guess field meanings. You MUST run annotation_script and visually inspect "
                "the saved JPEG images to determine what each FXXX collects. "
                "Calling save_field_mapping without viewing the images will produce incorrect results."
            ),
            "next_steps": [
                "1. ANNOTATE (required): Run the python script in 'annotation_script', replacing "
                "PDF_PATH with the actual file path. It writes orange FXXX labels onto the form "
                "and saves one JPEG per page to /tmp/fillform_page_N.jpg",
                "2. VIEW (required): Read every /tmp/fillform_page_N.jpg image. "
                "Each orange label shows exactly which field is which FXXX alias.",
                "3. IDENTIFY: From the images, record what each FXXX field collects.",
                "4. SAVE: Call save_field_mapping only after steps 1-3 are complete.",
            ],
            "annotation_script": annotation_script,
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    # -----------------------------------------------------------------------
    # MODE 2/3: resolve PDF from file_path or pdf_base64
    # -----------------------------------------------------------------------
    if file_path:
        fp = Path(file_path).expanduser()
        if not fp.exists():
            return [TextContent(type="text", text=(
                f"ERROR: file not found at '{file_path}'.\n"
                "This server cannot access local files on your machine. "
                "Use fields_json instead: run the python snippet from the tool "
                "description to extract field data locally, then pass the output here."
            ))]
        pdf_tmp = fp
        _own_tmp = False
    else:
        try:
            pdf_bytes = base64.b64decode(pdf_b64)
        except Exception as exc:
            return [TextContent(type="text", text=f"ERROR decoding pdf_base64: {exc}")]
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir="/tmp") as f:
            f.write(pdf_bytes)
            pdf_tmp = Path(f.name)
        _own_tmp = True

    try:
        structure = _structure_service.extract(pdf_tmp)
    except Exception as exc:
        if _own_tmp:
            pdf_tmp.unlink(missing_ok=True)
        return [TextContent(type="text", text=f"ERROR extracting structure: {exc}")]

    if not structure.field_widgets:
        if _own_tmp:
            pdf_tmp.unlink(missing_ok=True)
        return [TextContent(type="text", text=(
            "No AcroForm fields found. "
            "This tool requires a PDF with interactive AcroForm fields."
        ))]

    alias_map = _alias_registry.assign(structure.field_widgets)

    fields_data2: list[dict[str, Any]] = []
    for alias, widget in alias_map.field_widgets.items():
        nearby = _find_nearby_text(widget.bbox, widget.page, structure.text_blocks)
        page_dim = next(
            (pd for pd in structure.page_dimensions if pd.page == widget.page), None
        )
        position = _position_hint(widget.bbox, widget.page, page_dim)
        fields_data2.append({
            "alias": alias,
            "name": widget.name,
            "type": widget.field_type,
            "page": widget.page + 1,
            "bbox": [round(v, 1) for v in widget.bbox],
            "position": position,
            "nearby_text": nearby,
        })
    fields_data2.sort(key=lambda f: f["alias"])

    result2: dict[str, Any] = {
        "field_count": len(fields_data2),
        "page_count": len(structure.page_dimensions),
        "alias_map": alias_map.alias_to_field,
        "fields": fields_data2,
        "instructions": (
            "Review 'nearby_text' and 'position' to identify each field. "
            "Once all fields are identified, call save_field_mapping."
        ),
    }
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(result2, indent=2))
    ]

    if annotate_pages:
        annotated_tmp = Path(tempfile.mktemp(suffix="_annotated.pdf", dir="/tmp"))
        try:
            _annotator.annotate(pdf_tmp, alias_map, annotated_tmp)
            page_images = _render_pages(annotated_tmp)
            total = len(page_images)
            for i, img_b64 in enumerate(page_images):
                content.append(TextContent(type="text", text=f"--- Annotated page {i+1} of {total} ---"))
                content.append(ImageContent(type="image", data=img_b64, mimeType="image/jpeg"))
        except Exception as exc:
            content.append(TextContent(type="text", text=f"WARNING: annotation failed: {exc}"))
        finally:
            annotated_tmp.unlink(missing_ok=True)

    if _own_tmp:
        pdf_tmp.unlink(missing_ok=True)
    return content


# ---------------------------------------------------------------------------
# save_field_mapping implementation
# ---------------------------------------------------------------------------

async def _save_mapping(args: dict[str, Any]) -> list[TextContent]:
    form_family = str(args.get("form_family") or "unknown")
    version = str(args.get("version") or "1")

    try:
        alias_map_raw: dict[str, Any] = json.loads(args["alias_map_json"])
    except (json.JSONDecodeError, KeyError) as exc:
        return [TextContent(type="text", text=f"ERROR parsing alias_map_json: {exc}")]

    try:
        field_analysis: dict[str, dict[str, Any]] = json.loads(args["field_analysis_json"])
    except (json.JSONDecodeError, KeyError) as exc:
        return [TextContent(type="text", text=f"ERROR parsing field_analysis_json: {exc}")]

    # alias_map_json may be the full alias_to_field dict or the nested format
    if "alias_index" in alias_map_raw:
        alias_index: dict[str, str] = alias_map_raw["alias_index"]
    else:
        alias_index = {k: v for k, v in alias_map_raw.items() if isinstance(v, str)}

    fields: list[CanonicalField] = []
    for alias in sorted(alias_index.keys()):
        field_name = alias_index[alias]
        data = field_analysis.get(alias) or {}
        fields.append(CanonicalField(
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
        ))

    schema = CanonicalSchema(form_family=form_family, version=version, mode="acroform", fields=fields)
    schema_json = json.dumps(schema.to_dict(), indent=2)
    fill_script = schema.to_fill_script()

    fill_rules = (
        "═" * 60 + "\n"
        "FILL RULES — read before filling any field\n"
        "═" * 60 + "\n\n"
        "CHECKBOX RULES:\n"
        "  • To CHECK:   set field_value = on_state string (e.g. 'yes', 'Yes', 'amended')\n"
        "  • To UNCHECK: set field_value = 'Off'  (the literal string 'Off')\n"
        "  • NEVER set a checkbox to 'No', 'no', 'false', or '' — always 'Off'\n"
        "  • NEVER default empty/unused rows to 'No' — leave them as 'Off'\n"
        "    Example: debtor has 1 dependent → fill row 2a only, set rows 2b–2e to 'Off'\n\n"
        "DROPDOWN RULES:\n"
        "  • Set field_value to the EXACT string from choices[] — whitespace matters\n"
        "  • Wrong strings produce silent no-ops; the field appears blank in the filled PDF\n"
        "  • Example wrong: 'N. District of Florida'\n"
        "  • Example correct: 'Northern District of Florida'\n\n"
        "CONDITIONAL RULES:\n"
        "  • Only fill sub-fields when their parent condition is true\n"
        "  • Example: only fill 'check1a' text field when checkbox 'check1' = on_state\n"
        "  • Example: only fill dependent rows when has_dependents checkbox = on_state\n\n"
        "AFTER FILLING:\n"
        "  • Call validate_fill to verify your work before presenting to the user\n"
        "  • Run the STEP 1 snippet on the FILLED PDF to get actual values, then call validate_fill\n"
    )

    return [TextContent(type="text", text=(
        f"Schema ({len(fields)} fields):\n```json\n{schema_json}\n```\n\n"
        f"{'─' * 60}\n\n{fill_script}\n\n{fill_rules}"
    ))]


# ---------------------------------------------------------------------------
# validate_fill implementation
# ---------------------------------------------------------------------------

async def _validate_fill(args: dict[str, Any]) -> list[TextContent]:
    try:
        filled_fields: list[dict[str, Any]] = json.loads(args["filled_fields_json"])
    except (json.JSONDecodeError, KeyError) as exc:
        return [TextContent(type="text", text=f"ERROR parsing filled_fields_json: {exc}")]

    try:
        intended: dict[str, Any] = json.loads(args["intended_values"])
    except (json.JSONDecodeError, KeyError) as exc:
        return [TextContent(type="text", text=f"ERROR parsing intended_values: {exc}")]

    try:
        alias_map_raw: dict[str, Any] = json.loads(args["alias_map_json"])
    except (json.JSONDecodeError, KeyError) as exc:
        return [TextContent(type="text", text=f"ERROR parsing alias_map_json: {exc}")]

    schema_raw: dict[str, Any] = {}
    schema_str = args.get("schema_json") or ""
    if schema_str:
        try:
            schema_raw = json.loads(schema_str)
        except json.JSONDecodeError:
            pass

    # Build alias → field_name map
    if "alias_index" in alias_map_raw:
        alias_index: dict[str, str] = alias_map_raw["alias_index"]
    else:
        alias_index = {k: v for k, v in alias_map_raw.items() if isinstance(v, str)}

    # Build field_name → filled field dict
    filled_by_name: dict[str, dict[str, Any]] = {
        str(f.get("name") or ""): f for f in filled_fields
    }

    # Extract schema field info if available
    schema_fields: dict[str, dict[str, Any]] = {}
    for sf in schema_raw.get("fields", []):
        schema_fields[sf.get("alias", "")] = sf

    issues: list[dict[str, Any]] = []
    correct = 0
    total = len(intended)

    for alias, intended_val in intended.items():
        field_name = alias_index.get(alias, "")
        filled_f = filled_by_name.get(field_name, {})
        actual_val = filled_f.get("value")
        ftype = filled_f.get("type") or schema_fields.get(alias, {}).get("field_type", "Text")
        sf = schema_fields.get(alias, {})

        # Normalize for comparison
        actual_str = str(actual_val) if actual_val is not None else ""
        intended_str = str(intended_val) if intended_val is not None else ""

        field_issues: list[dict[str, Any]] = []

        if ftype == "CheckBox":
            on_state = filled_f.get("on_state") or sf.get("on_state") or ""
            # phantom_check: field is checked but intended is "Off" or not provided
            if actual_str not in ("", "Off", None) and intended_str in ("", "Off"):
                field_issues.append({
                    "severity": "error",
                    "issue": "phantom_check",
                    "detail": f"Field is set to '{actual_str}' but should be 'Off'",
                })
            # wrong on_state: field is checked but with wrong value
            elif intended_str not in ("", "Off") and actual_str not in ("", "Off"):
                if on_state and actual_str != on_state:
                    field_issues.append({
                        "severity": "error",
                        "issue": "wrong_on_state",
                        "detail": f"Used '{actual_str}' but on_state is '{on_state}'",
                    })
            # checked 'no'/'No'/'false' instead of 'Off'
            if intended_str in ("Off", "") and actual_str.lower() in ("no", "false"):
                field_issues.append({
                    "severity": "error",
                    "issue": "wrong_off_value",
                    "detail": f"Used '{actual_str}' for unchecked — must use 'Off'",
                })

        elif ftype in ("ComboBox", "ListBox"):
            choices = filled_f.get("choices") or sf.get("choices") or []
            if actual_str and choices and actual_str not in choices:
                field_issues.append({
                    "severity": "error",
                    "issue": "invalid_choice",
                    "detail": (
                        f"'{actual_str}' not in choices. "
                        f"Valid options: {choices[:5]}{'…' if len(choices) > 5 else ''}"
                    ),
                })

        # unchanged_from_blank: intended non-empty but field still blank
        if intended_str not in ("", "Off") and actual_str in ("", None):
            field_issues.append({
                "severity": "error",
                "issue": "unchanged_from_blank",
                "detail": f"Field still blank — value '{intended_str}' was not applied",
            })

        # value mismatch (non-checkbox, non-blank)
        if not field_issues and actual_str != intended_str and ftype != "CheckBox":
            if intended_str not in ("", "Off") and actual_str not in ("", "Off"):
                field_issues.append({
                    "severity": "warning",
                    "issue": "value_mismatch",
                    "detail": f"Expected '{intended_str}', got '{actual_str}'",
                })

        if field_issues:
            for fi in field_issues:
                fi.update({"alias": alias, "field_name": field_name, "type": ftype,
                           "intended": intended_str, "actual": actual_str})
                issues.append(fi)
        else:
            correct += 1

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    passed = len(errors) == 0

    refinement = ""
    if errors:
        lines = [f"Fix {len(errors)} error(s) before presenting to user:"]
        for i, err in enumerate(errors[:10], 1):
            lines.append(
                f"{i}. {err['alias']} ({err['field_name']}): {err['issue']} — {err['detail']}"
            )
        if len(errors) > 10:
            lines.append(f"   … and {len(errors) - 10} more errors")
        refinement = "\n".join(lines)

    report: dict[str, Any] = {
        "passed": passed,
        "score": f"{correct}/{total} fields correct",
        "errors": len(errors),
        "warnings": len(warnings),
        "issues": issues,
        "refinement_instructions": refinement or "All checks passed.",
    }

    return [TextContent(type="text", text=json.dumps(report, indent=2))]


# ---------------------------------------------------------------------------
# Helpers: nearby-text extraction and position hints
# ---------------------------------------------------------------------------

def _build_annotation_script(fields_data: list[dict[str, Any]]) -> str:
    """Return a self-contained python script Claude can run locally.

    Overlays color-coded FXXX labels on the original PDF at 2.5× DPI and
    saves one JPEG per page to /tmp/fillform_page_N.jpg.

    Color scheme:
      Text       → blue fill, white label centered inside rect
      CheckBox   → green ring (no fill), green pill label to the RIGHT
      RadioButton→ same as CheckBox
      ComboBox   → orange fill, white label
      ListBox    → orange fill, white label
      Button     → purple fill, white label (agent should skip these)
    """
    fields_json = json.dumps(fields_data)
    return (
        "# Run locally — replace PDF_PATH with the actual file path.\n"
        "# Saves /tmp/fillform_page_N.jpg at 2.5x DPI with color-coded FXXX labels.\n"
        "# Legend (for reference — NOT burned into image):\n"
        "#   Blue   = Text field       Orange = ComboBox/ListBox\n"
        "#   Green  = CheckBox/Radio   Purple = Button (skip)\n"
        "import json, sys\n"
        "PDF_PATH = '/mnt/user-data/uploads/YOUR_FILE.pdf'  # <-- change this\n"
        "OUT_DIR  = '/tmp'\n"
        f"FIELDS = {fields_json}\n"
        "\n"
        "try:\n"
        "    import fitz\n"
        "except ImportError:\n"
        "    sys.exit('PyMuPDF (fitz) required: pip install pymupdf')\n"
        "\n"
        "# (border_color, fill_color) — fill=None means ring only\n"
        "COLORS = {\n"
        "    'Text':        ((0.05, 0.25, 0.75), (0.1,  0.4,  0.9 )),\n"
        "    'CheckBox':    ((0.0,  0.55, 0.15), None),\n"
        "    'RadioButton': ((0.0,  0.55, 0.15), None),\n"
        "    'ComboBox':    ((0.7,  0.25, 0.0 ), (1.0,  0.45, 0.0 )),\n"
        "    'ListBox':     ((0.7,  0.25, 0.0 ), (1.0,  0.45, 0.0 )),\n"
        "    'Button':      ((0.35, 0.05, 0.55), (0.5,  0.1,  0.7 )),\n"
        "}\n"
        "DEFAULT_COLORS = ((0.5, 0.5, 0.0), (0.8, 0.8, 0.0))\n"
        "\n"
        "def draw_label(page, x, y, label, fs, color=(1, 1, 1)):\n"
        "    # Shadow pass for readability over any background\n"
        "    for dx, dy in [(-0.4,0),(0.4,0),(0,-0.4),(0,0.4)]:\n"
        "        page.insert_text((x+dx, y+dy), label, fontsize=fs, color=(0, 0, 0.3))\n"
        "    page.insert_text((x, y), label, fontsize=fs, color=color)\n"
        "\n"
        "doc = fitz.open(PDF_PATH)\n"
        "for f in FIELDS:\n"
        "    page = doc[f['page'] - 1]\n"
        "    x0, y0, x1, y1 = f['bbox']\n"
        "    rect = fitz.Rect(x0, y0, x1, y1)\n"
        "    ftype = f.get('type', 'Text')\n"
        "    border_c, fill_c = COLORS.get(ftype, DEFAULT_COLORS)\n"
        "    alias = f['alias']\n"
        "    fs = max(5, min(8, (y1 - y0) * 0.65))\n"
        "\n"
        "    if ftype in ('CheckBox', 'RadioButton'):\n"
        "        # Ring only — preserve the checkbox visual, label goes to the right\n"
        "        page.draw_rect(rect, color=border_c, fill=None, width=1.2)\n"
        "        tw = fitz.get_text_length(alias, fontsize=fs)\n"
        "        px0 = x1 + 2\n"
        "        py0, py1 = y0, y1\n"
        "        pill = fitz.Rect(px0 - 1, py0, px0 + tw + 3, py1)\n"
        "        page.draw_rect(pill, color=border_c, fill=(0.85, 1.0, 0.87), width=0.5)\n"
        "        ty = py0 + (py1 - py0 + fs) / 2 - 1\n"
        "        draw_label(page, px0 + 1, ty, alias, fs, color=(0.0, 0.4, 0.1))\n"
        "    else:\n"
        "        page.draw_rect(rect, color=border_c, fill=fill_c, width=0.5)\n"
        "        tw = fitz.get_text_length(alias, fontsize=fs)\n"
        "        tx = x0 + max(0, (x1 - x0 - tw) / 2)\n"
        "        ty = y0 + (y1 - y0 + fs) / 2 - 1\n"
        "        draw_label(page, tx, ty, alias, fs)\n"
        "\n"
        "mat = fitz.Matrix(2.5, 2.5)  # 180 DPI — labels readable on small checkbox fields\n"
        "saved = []\n"
        "for i, page in enumerate(doc):\n"
        "    pix = page.get_pixmap(matrix=mat, alpha=False)\n"
        "    out = f'{OUT_DIR}/fillform_page_{i+1}.jpg'\n"
        "    pix.save(out, jpg_quality=85)\n"
        "    saved.append(out)\n"
        "print('Saved:', saved)\n"
    )


def _find_nearby_text(
    bbox: tuple[float, float, float, float],
    page: int,
    text_blocks: list[TextBlock],
    max_dist: float = 60.0,
) -> str:
    """Return the text of the nearest text block to *bbox* on the same page.

    Preference order (lower score = better):
    1. Blocks whose vertical centre aligns with the field (same row)
    2. Blocks to the left of the field (typical label position)
    3. Blocks above the field
    Blocks further than *max_dist* points are ignored.
    """
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

        # Euclidean distance between centres
        dist = math.hypot(f_cx - b_cx, f_cy - b_cy)
        if dist > max_dist:
            continue

        # Vertical alignment bonus: blocks on the same row score better
        vert_offset = abs(f_cy - b_cy)
        # Prefer labels to the left of the field
        left_bonus = 0.0 if bx1 <= fx0 + 5 else 20.0

        score = dist + vert_offset * 0.5 + left_bonus
        if score < best_score:
            best_score = score
            best_text = text

    # Truncate long blocks (e.g. full paragraphs near a field)
    if len(best_text) > 120:
        best_text = best_text[:120].rsplit(" ", 1)[0] + "…"
    return best_text


def _position_hint(
    bbox: tuple[float, float, float, float],
    page: int,
    page_dim: Any,  # PageDimensions | None
) -> str:
    """Return a human-readable position string like 'page 2, upper-left'."""
    if page_dim is None:
        return _position_hint_raw(bbox, page)
    cx = (bbox[0] + bbox[2]) / 2 / page_dim.width
    cy = (bbox[1] + bbox[3]) / 2 / page_dim.height
    horiz = "left" if cx < 0.4 else ("right" if cx > 0.6 else "center")
    vert = "upper" if cy < 0.33 else ("lower" if cy > 0.66 else "middle")
    return f"page {page + 1}, {vert}-{horiz}"


def _position_hint_raw(
    bbox: tuple[float, float, float, float],
    page: int,
    page_width: float = 612.0,
    page_height: float = 792.0,
) -> str:
    """Position hint without page dimensions — uses US Letter defaults."""
    cx = (bbox[0] + bbox[2]) / 2 / page_width
    cy = (bbox[1] + bbox[3]) / 2 / page_height
    horiz = "left" if cx < 0.4 else ("right" if cx > 0.6 else "center")
    vert = "upper" if cy < 0.33 else ("lower" if cy > 0.66 else "middle")
    return f"page {page + 1}, {vert}-{horiz}"


# ---------------------------------------------------------------------------
# Optional image rendering (only used when annotate_pages=True)
# ---------------------------------------------------------------------------

def _render_pages(pdf_path: Path, dpi: int = 72) -> list[str]:
    import fitz
    images: list[str] = []
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    with fitz.open(str(pdf_path)) as doc:
        for i in range(doc.page_count):
            pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
            images.append(base64.standard_b64encode(pix.tobytes("jpg", jpg_quality=80)).decode())
    return images


# ---------------------------------------------------------------------------
# ASGI app  (Vercel detects the `app` variable)
# ---------------------------------------------------------------------------

class _App:
    def _header_map(self, scope) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            try:
                out[k.decode("latin-1").lower()] = v.decode("latin-1")
            except Exception:
                continue
        return out

    def _base_url(self, scope) -> str:
        headers = self._header_map(scope)
        proto = headers.get("x-forwarded-proto") or scope.get("scheme") or "https"
        host = headers.get("x-forwarded-host") or headers.get("host") or "localhost"
        return f"{proto}://{host}"

    async def _send_json(self, send, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _send_html(self, send, html: str, status: int = 200) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/html; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": html.encode("utf-8")})

    def _analytics_payload(self, refresh: bool = False) -> dict[str, Any]:
        now = int(time.time())
        cached_payload = _analytics_cache.get("payload")
        cached_ts = int(_analytics_cache.get("ts", 0) or 0)
        if (not refresh) and cached_payload and (now - cached_ts) < _ANALYTICS_TTL_SECONDS:
            return dict(cached_payload)

        state_path = Path("/tmp/fillform_bankruptcy_state.json")
        out_dir = Path("/tmp/fillform_bankruptcy_forms")
        manifest_path: Path | None = None
        result = None

        if (not refresh) and state_path.exists():
            try:
                state_obj = json.loads(state_path.read_text(encoding="utf-8"))
                latest = state_obj.get("latest_manifest_path")
                if latest:
                    candidate = Path(str(latest))
                    if candidate.exists():
                        manifest_path = candidate
            except Exception:
                manifest_path = None

        if manifest_path is None and not refresh:
            return self._index_catalogue_payload()

        if manifest_path is None:
            try:
                result = self._run_sync_with_timeout(out_dir=out_dir, state_path=state_path, timeout_seconds=8.0)
                if result is not None:
                    manifest_path = Path(result.manifest_path)
            except Exception:
                if state_path.exists():
                    try:
                        state_obj = json.loads(state_path.read_text(encoding="utf-8"))
                        latest = state_obj.get("latest_manifest_path")
                        if latest:
                            candidate = Path(str(latest))
                            if candidate.exists():
                                manifest_path = candidate
                    except Exception:
                        manifest_path = None
                if manifest_path is None:
                    return self._index_catalogue_payload()

        forms = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path and manifest_path.exists() else {}
        rows = []
        for slug, record in sorted(forms.items()):
            if not isinstance(record, dict):
                continue
            rows.append(
                {
                    "slug": slug,
                    "pdf_url": record.get("pdf_url"),
                    "page_url": record.get("page_url"),
                    "published_at": record.get("pdf_last_modified") or "",
                    "updated_on": record.get("updated_on") or "",
                    "effective_on": record.get("effective_on") or "",
                    "form_number": record.get("form_number") or "",
                    "doc_type": self._doc_type_from_url(str(record.get("pdf_url") or "")),
                }
            )
        analytics = self._build_extended_analytics(rows)
        payload = {
            "generated_at_unix": int(time.time()),
            "generated_at_iso": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "forms_in_index": (result.total_index_forms if result else None),
                "pdf_records": len(rows),
                "added": (len(result.added) if result else 0),
                "removed": (len(result.removed) if result else 0),
                "changed": (len(result.changed) if result else 0),
            },
            "added": (result.added if result else []),
            "removed": (result.removed if result else []),
            "changed": (result.changed if result else []),
            "source": ("manifest" if result else "manifest-cache"),
            "analytics": analytics,
            "forms": rows,
        }
        _analytics_cache["ts"] = now
        _analytics_cache["payload"] = dict(payload)
        return payload

    def _run_sync_with_timeout(
        self,
        out_dir: Path,
        state_path: Path,
        timeout_seconds: float,
    ):
        def _do_sync():
            syncer = USCourtsBankruptcyFormsSync(min_request_interval_seconds=1.5)
            return syncer.sync(output_dir=out_dir, state_path=state_path, download_pdfs=False)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_do_sync)
            try:
                return future.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError:
                return None

    def _index_catalogue_payload(self) -> dict[str, Any]:
        """Fast fallback payload based only on the bankruptcy index page."""
        syncer = USCourtsBankruptcyFormsSync(min_request_interval_seconds=0.5)
        html, _cache = syncer._get_text(BANKRUPTCY_INDEX_URL, {})
        pages = syncer._extract_form_pages(html)
        rows = [
            {
                "slug": syncer._slug_from_page(page_url),
                "pdf_url": page_url,
                "page_url": page_url,
                "published_at": "",
                "updated_on": "",
                "effective_on": "",
                "form_number": "",
                "doc_type": self._doc_type_from_url(page_url),
            }
            for page_url in pages
        ]
        return {
            "generated_at_unix": int(time.time()),
            "generated_at_iso": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "forms_in_index": len(pages),
                "pdf_records": len(rows),
                "added": 0,
                "removed": 0,
                "changed": 0,
            },
            "added": [],
            "removed": [],
            "changed": [],
            "analytics": self._build_extended_analytics(rows),
            "forms": rows,
            "source": "index_fallback",
        }

    def _doc_type_from_url(self, url: str) -> str:
        lower = url.lower()
        if lower.endswith(".pdf") and ("_ins" in lower or "instruction" in lower):
            return "instruction_pdf"
        if "/forms-rules/forms/" in lower:
            return "form_page"
        if lower.endswith(".pdf"):
            return "form_pdf"
        return "other"

    def _build_extended_analytics(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        chapter_counts = {"7": 0, "11": 0, "12": 0, "13": 0}
        schedule_count = 0
        published_count = 0
        updated_on_count = 0
        doc_type_counts: dict[str, int] = {}
        for row in rows:
            slug = str(row.get("slug") or "").lower()
            if slug.startswith("schedule-"):
                schedule_count += 1
            m = re.search(r"chapter-(7|11|12|13)", slug)
            if m:
                chapter_counts[m.group(1)] += 1
            if row.get("published_at"):
                published_count += 1
            if row.get("updated_on"):
                updated_on_count += 1
            dtype = str(row.get("doc_type") or "unknown")
            doc_type_counts[dtype] = doc_type_counts.get(dtype, 0) + 1

        return {
            "schedule_records": schedule_count,
            "published_header_records": published_count,
            "updated_on_records": updated_on_count,
            "chapter_counts": chapter_counts,
            "doc_type_counts": doc_type_counts,
            "unique_page_count": len({str(r.get("page_url") or "") for r in rows}),
        }

    def _home_html(self, base_url: str, initial_payload: dict[str, Any] | None = None) -> str:
        initial_payload = initial_payload or {"counts": {}, "added": [], "changed": [], "forms": []}
        counts_text = json.dumps(initial_payload.get("counts", {}), indent=2)
        summary_text = f"{counts_text}\\nAdded: {', '.join(initial_payload.get('added', []))}\\nChanged: {', '.join(initial_payload.get('changed', []))}"
        rows_html = []
        for row in initial_payload.get("forms", []):
            if not isinstance(row, dict):
                continue
            slug = html.escape(str(row.get("slug", "")))
            form_number = html.escape(str(row.get("form_number", "")))
            updated_on = html.escape(str(row.get("updated_on", "")))
            published = html.escape(str(row.get("published_at", "")))
            pdf_url = html.escape(str(row.get("pdf_url", "")))
            rows_html.append(
                f"<tr><td>{slug}</td><td>{form_number}</td><td>{updated_on}</td><td>{published}</td><td><a href='{pdf_url}' target='_blank'>open</a></td></tr>"
            )
        rows_markup = "".join(rows_html)

        html = """<!doctype html>
<html><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width,initial-scale=1'/>
<title>FillForm Bankruptcy MCP</title>
<style>
body{font-family:Inter,system-ui,sans-serif;margin:2rem;max-width:1100px}
code,pre{background:#f5f5f5;padding:.2rem .4rem;border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid #ddd;padding:.45rem;vertical-align:top}
th{background:#fafafa;text-align:left}
.muted{color:#666}
</style></head>
<body>
<h1>FillForm — Bankruptcy MCP Setup</h1>
<p class='muted'>Simple Vercel landing page with MCP setup + live bankruptcy form analytics.</p>
<h2>1) MCP Setup</h2>
<p>Use this URL for your MCP server:</p>
<pre><code>__BASE_URL__</code></pre>
<p>Claude settings snippet:</p>
<pre><code>{
  "mcpServers": {
    "fillform": { "url": "__BASE_URL__" }
  }
}</code></pre>
<h2>2) Tutorial — Get any bankruptcy doc</h2>
<ol>
  <li>Call <code>extract_form_fields</code> with local field JSON or a PDF input.</li>
  <li>Review aliases and map each field meaning.</li>
  <li>Call <code>save_field_mapping</code> to create schema + fill guide.</li>
  <li>Fill and then call <code>validate_fill</code> to QA the output.</li>
</ol>
<h2>3) Live bankruptcy form analytics</h2>
<p class='muted'>This checks USCourts, computes diffs, and shows publish headers when available (cached up to 5 minutes).</p>
<button onclick='load(true)'>Refresh analytics</button>
<pre id='summary'>__SUMMARY__</pre>
<pre id='details'>__DETAILS__</pre>
<table><thead><tr><th>Form Key</th><th>Form #</th><th>Updated on</th><th>Published (header)</th><th>PDF</th></tr></thead><tbody id='rows'>__ROWS__</tbody></table>
<script>
async function load(force){
  try{
    const refresh = force ? '1' : '0';
    const res=await fetch('/bankruptcy-analytics.json?refresh='+refresh,{cache:'no-store'});
    const data=await res.json();
    if(!data.ok){ document.getElementById('summary').textContent='Analytics error: '+(data.error||'unknown'); return; }
    document.getElementById('summary').textContent=JSON.stringify(data.counts,null,2)+
      "\\nAdded: "+data.added.join(', ')+"\\nChanged: "+data.changed.join(', ');
    document.getElementById('details').textContent=JSON.stringify(data.analytics||{},null,2);
    const rows=document.getElementById('rows'); rows.innerHTML='';
    data.forms.forEach(f=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${f.slug}</td><td>${f.form_number||''}</td><td>${f.updated_on||''}</td><td>${f.published_at||''}</td><td><a href='${f.pdf_url}' target='_blank'>open</a></td>`;
      rows.appendChild(tr);
    });
  }catch(err){
    document.getElementById('summary').textContent='Analytics request failed: '+String(err);
    document.getElementById('details').textContent='';
  }
}
if(!document.getElementById('rows').children.length){ load(false); }
</script></body></html>"""
        details_text = json.dumps(initial_payload.get("analytics", {}), indent=2)
        return (
            html.replace("__BASE_URL__", base_url)
            .replace("__SUMMARY__", summary_text)
            .replace("__DETAILS__", details_text)
            .replace("__ROWS__", rows_markup)
        )

    async def _read_body(self, receive) -> bytes:
        """Read full request body from ASGI receive channel."""
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        return body

    async def _handle_bankruptcy_sync(self, receive, send) -> None:
        """Handle POST /bankruptcy-forms/sync — run crawler and return manifest inline.

        On Vercel, the full crawl (60+ pages + sitemap) exceeds function timeout.
        This uses a thread-pool timeout to return partial results if the crawl
        doesn't finish in time, and falls back to the index-only catalogue.
        """
        body = await self._read_body(receive)
        payload: dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                await self._send_json(send, {"ok": False, "error": "Invalid JSON body"}, status=400)
                return

        out_dir = Path("/tmp/fillform_bankruptcy_forms")
        state_path = Path("/tmp/fillform_bankruptcy_state.json")
        interval = float(payload.get("min_request_interval_seconds", 1.2))
        max_pages = payload.get("max_form_pages")
        if max_pages is not None:
            max_pages = int(max_pages)

        def _do_sync():
            syncer = USCourtsBankruptcyFormsSync(min_request_interval_seconds=interval)
            return syncer.sync(
                output_dir=out_dir,
                state_path=state_path,
                download_pdfs=False,
                max_form_pages=max_pages,
            )

        # Run with timeout to stay within Vercel's function limits.
        # Hobby plan: 10s, Pro: 60s. Use 8s to leave room for response serialization.
        sync_result = None
        timed_out = False
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_do_sync)
                try:
                    from dataclasses import asdict
                    sync_result = asdict(future.result(timeout=8.0))
                except concurrent.futures.TimeoutError:
                    timed_out = True
        except Exception as exc:
            await self._send_json(send, {"ok": False, "error": f"Sync failed: {exc}"}, status=502)
            return

        # Load the manifest — either from the completed sync or from cached state.
        manifest: dict[str, Any] = {}
        if sync_result:
            manifest_path_str = sync_result.get("manifest_path", "")
            if manifest_path_str:
                mp = Path(manifest_path_str)
                if mp.exists():
                    try:
                        manifest = json.loads(mp.read_text(encoding="utf-8"))
                    except Exception:
                        pass
            sync_result["manifest"] = manifest
            await self._send_json(send, {"ok": True, "result": sync_result})
        elif timed_out:
            # Sync didn't complete — try returning cached manifest from state file.
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    latest = state.get("latest_manifest_path")
                    if latest and Path(latest).exists():
                        manifest = json.loads(Path(latest).read_text(encoding="utf-8"))
                except Exception:
                    pass
            # Fall back to index-only catalogue if no manifest available.
            # Use _index_catalogue_payload directly — it only fetches the index page
            # (single HTTP request) and is fast enough for the remaining timeout budget.
            if not manifest:
                try:
                    catalogue = self._index_catalogue_payload()
                except Exception:
                    catalogue = {"forms": []}
                for row in catalogue.get("forms", []):
                    slug = row.get("slug", "")
                    if slug:
                        manifest[slug] = {
                            "slug": slug,
                            "page_url": row.get("page_url", ""),
                            "pdf_url": row.get("pdf_url", ""),
                            "file_name": f"{slug}.pdf",
                            "sha256": "",
                            "size_bytes": 0,
                            "pdf_etag": "",
                            "pdf_last_modified": row.get("published_at", ""),
                        }
            await self._send_json(send, {
                "ok": True,
                "partial": True,
                "result": {"manifest_path": "", "manifest": manifest,
                           "total_index_forms": len(manifest), "total_pdf_forms": len(manifest),
                           "downloaded_files": 0, "unchanged_files": 0, "reused_without_fetch": 0,
                           "added": [], "removed": [], "changed": []},
            })
        else:
            await self._send_json(send, {"ok": False, "error": "Sync produced no result"}, status=502)

    async def _handle_bankruptcy_manifest(self, send) -> None:
        """Handle GET /bankruptcy-forms/manifest — return latest cached manifest."""
        state_path = Path("/tmp/fillform_bankruptcy_state.json")
        if not state_path.exists():
            await self._send_json(send, {"ok": False, "error": "No manifest available. Run sync first."}, status=404)
            return

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            manifest_path = state.get("latest_manifest_path")
            if not manifest_path:
                await self._send_json(send, {"ok": False, "error": "No manifest path in state."}, status=404)
                return
            manifest_file = Path(manifest_path)
            if not manifest_file.exists():
                await self._send_json(send, {"ok": False, "error": "Manifest file not found."}, status=404)
                return
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            await self._send_json(send, {"ok": True, "manifest": manifest})
        except Exception as exc:
            await self._send_json(send, {"ok": False, "error": str(exc)}, status=500)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        path = scope.get("path", "/")
        method = scope.get("method", "")

        # Bankruptcy form sync routes
        if path == "/bankruptcy-forms/sync" and method == "POST":
            await self._handle_bankruptcy_sync(receive, send)
            return
        if path == "/bankruptcy-forms/manifest" and method == "GET":
            await self._handle_bankruptcy_manifest(send)
            return
        if path == "/health" and method == "GET":
            await self._send_json(send, {"ok": True, "service": "fillform-bankruptcy-mcp"})
            return

        if method == "GET":
            if path in ("/", "/index.html"):
                await self._send_html(send, self._home_html(self._base_url(scope), None), status=200)
                return
            if path == "/bankruptcy-analytics.json":
                query = parse_qs((scope.get("query_string") or b"").decode("utf-8", "ignore"))
                refresh = str((query.get("refresh") or ["0"])[0]).lower() in ("1", "true", "yes")
                try:
                    payload = self._analytics_payload(refresh=refresh)
                except Exception as exc:
                    await self._send_json(send, {"ok": False, "error": str(exc)}, status=502)
                    return
                await self._send_json(send, {"ok": True, **payload}, status=200)
                return
            await self._send_json(send, {"ok": False, "error": "Not found"}, status=404)
            return
        mgr = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
        async with mgr.run():
            await mgr.handle_request(scope, receive, send)


app = _App()
