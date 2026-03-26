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
import json
import math
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent / "src"))

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import ImageContent, TextContent, Tool

from fillform.annotator import PdfAnnotator
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


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="extract_form_fields",
            description=(
                "Assign sequential FXXX aliases (F001, F002, …) to AcroForm fields and "
                "return a compact JSON object with the alias map and per-field metadata "
                "(page, bbox, type, position). Response is text-only JSON — no images.\n\n"
                "THREE input modes — use the first one that works:\n\n"
                "  MODE 1 — fields_json (BEST, zero context cost):\n"
                "    Extract field data locally with python, pass the JSON string.\n"
                "    Run this python one-liner first (no output shown in conversation):\n"
                "      python3 -c \"\n"
                "import json, sys\n"
                "try:\n"
                "    import fitz\n"
                "    doc = fitz.open('/mnt/user-data/uploads/YOUR_FILE.pdf')\n"
                "    out = [{'name': w.field_name, 'type': w.field_type_string,\n"
                "            'page': i, 'bbox': list(w.rect)}\n"
                "           for i, p in enumerate(doc) for w in (p.widgets() or [])]\n"
                "except ImportError:\n"
                "    from pypdf import PdfReader\n"
                "    r = PdfReader('/mnt/user-data/uploads/YOUR_FILE.pdf')\n"
                "    out = [{'name': k, 'type': 'Tx', 'page': 0, 'bbox': [0,0,100,20]}\n"
                "           for k in (r.get_fields() or {})]\n"
                "sys.stdout.write(json.dumps(out))\n"
                "      \"\n"
                "    Then pass the output as fields_json. This outputs ~5KB, not the whole PDF.\n\n"
                "  MODE 2 — file_path: server reads the file directly.\n"
                "    Works only if the server can access the path (local server only).\n\n"
                "  MODE 3 — pdf_base64: AVOID unless modes 1 and 2 both fail.\n"
                "    Do NOT run 'base64 <file>' in bash — that prints the full file into the\n"
                "    conversation and causes context overflow. This mode is only viable for\n"
                "    PDFs under ~200KB."
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
                "Call this after you have identified what every FXXX field collects."
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
                            '"expected_format":"…or null","is_required":true|false,"section":"…or null"}, …}'
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
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent | ImageContent]:
    if name == "extract_form_fields":
        return await _extract_fields(arguments)
    if name == "save_field_mapping":
        return await _save_mapping(arguments)
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

        fields_data: list[dict[str, Any]] = []
        for alias, widget in alias_map.field_widgets.items():
            position = _position_hint_raw(widget.bbox, widget.page)
            fields_data.append({
                "alias": alias,
                "name": widget.name,
                "type": widget.field_type,
                "page": widget.page + 1,
                "bbox": [round(v, 1) for v in widget.bbox],
                "position": position,
            })
        fields_data.sort(key=lambda f: f["alias"])

        result: dict[str, Any] = {
            "field_count": len(fields_data),
            "alias_map": alias_map.alias_to_field,
            "fields": fields_data,
            "instructions": (
                "Use the original PDF attachment in your conversation to identify what "
                "each field collects. Match fields by their 'position' and 'name'. "
                "Once all fields are identified, call save_field_mapping."
            ),
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

    return [TextContent(type="text", text=(
        f"Schema ({len(fields)} fields):\n```json\n{schema_json}\n```\n\n"
        f"{'─' * 60}\n\n{fill_script}"
    ))]


# ---------------------------------------------------------------------------
# Helpers: nearby-text extraction and position hints
# ---------------------------------------------------------------------------

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
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        # Per MCP streamable-HTTP spec: 405 on GET tells clients to use POST-only mode.
        # Required for serverless — no persistent SSE connections possible.
        if scope.get("method") == "GET":
            await send({
                "type": "http.response.start",
                "status": 405,
                "headers": [(b"content-type", b"application/json"), (b"allow", b"POST, DELETE")],
            })
            await send({"type": "http.response.body", "body": b""})
            return
        mgr = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
        async with mgr.run():
            await mgr.handle_request(scope, receive, send)


app = _App()
