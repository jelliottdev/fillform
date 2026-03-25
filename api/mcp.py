"""Vercel-compatible MCP server for fillform.

Differences from the local mcp.py server:
- Tools accept ``pdf_base64`` (base64-encoded PDF bytes) instead of a file path
  because Vercel functions have no access to the caller's local filesystem.
- All intermediate files are written to ``/tmp`` (the only writable path on Vercel).
- ``save_field_mapping`` returns the schema JSON and fill script as tool response
  text rather than writing to persistent disk storage.
- Uses ``StreamableHTTPSessionManager(stateless=True)`` so every request gets a
  fresh transport — required for serverless where there is no persistent process.

Claude Code URL config::

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
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

# Make the local fillform package importable when running on Vercel
# (src/ is sibling of api/ in the repo root)
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent / "src"))

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import ImageContent, TextContent, Tool

from fillform.annotator import PdfAnnotator
from fillform.contracts import CanonicalField, CanonicalSchema
from fillform.field_alias import FieldAliasRegistry
from fillform.structure import PdfStructureService, PyMuPdfStructureAdapter

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("fillform")

_structure_service = PdfStructureService(adapter=PyMuPdfStructureAdapter())
_alias_registry = FieldAliasRegistry()
_annotator = PdfAnnotator()


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="prepare_form_for_analysis",
            description=(
                "Extract AcroForm fields from a PDF, assign sequential FXXX aliases "
                "(F001, F002, …), overlay those labels in vibrant orange, and return "
                "the alias key-mapping JSON plus JPEG images of the annotated pages. "
                "IMPORTANT: prefer file_path over pdf_base64 — passing a file path "
                "avoids base64-encoding the file in the conversation (which causes "
                "context overflow). Only fall back to pdf_base64 if the file is not "
                "accessible by path. To avoid context overflow on long PDFs, use "
                "page_start/page_end to request pages in small batches."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to the PDF file on the server's filesystem. "
                            "Use this instead of pdf_base64 whenever possible — it avoids "
                            "printing the file contents into the conversation. "
                            "Example: /mnt/user-data/uploads/form.pdf"
                        ),
                    },
                    "pdf_base64": {
                        "type": "string",
                        "description": "Base64-encoded PDF file content. Only use if file_path is not available.",
                    },
                    "page_start": {
                        "type": "integer",
                        "description": "1-based first page to return (default 1).",
                        "default": 1,
                    },
                    "page_end": {
                        "type": "integer",
                        "description": "1-based last page to return inclusive (default: all pages, max 5 at a time).",
                    },
                    "dpi": {
                        "type": "integer",
                        "description": "Rendering resolution (default 72). Increase only if labels are unreadable.",
                        "default": 72,
                    },
                },
                "required": ["pdf_base64"],
            },
        ),
        Tool(
            name="save_field_mapping",
            description=(
                "Accepts Claude's field analysis JSON and the alias map from "
                "prepare_form_for_analysis. Returns the complete CanonicalSchema JSON "
                "and a plain-text AI fill guide (fill script)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "alias_map_json": {
                        "type": "string",
                        "description": "The alias_map JSON string from prepare_form_for_analysis.",
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


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent | ImageContent]:
    if name == "prepare_form_for_analysis":
        return await _prepare_form(arguments)
    if name == "save_field_mapping":
        return await _save_mapping(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _prepare_form(args: dict[str, Any]) -> list[TextContent | ImageContent]:
    dpi = int(args.get("dpi") or 72)
    page_start = max(1, int(args.get("page_start") or 1))
    page_end_arg = args.get("page_end")

    file_path = args.get("file_path") or ""
    pdf_b64 = args.get("pdf_base64") or ""

    if not file_path and not pdf_b64:
        return [TextContent(type="text", text="ERROR: provide file_path or pdf_base64.")]

    # Resolve PDF bytes from either source
    if file_path:
        fp = Path(file_path).expanduser()
        if not fp.exists():
            return [TextContent(
                type="text",
                text=(
                    f"ERROR: file not found at '{file_path}'.\n"
                    "If you are on claude.ai, uploaded files are at "
                    "/mnt/user-data/uploads/<filename>. Make sure you pass the "
                    "exact path shown when the file was uploaded."
                ),
            )]
        pdf_bytes = fp.read_bytes()
        # Use the original file directly — no temp copy needed
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

    annotated_tmp = Path(tempfile.mktemp(suffix="_annotated.pdf", dir="/tmp"))

    try:
        structure = _structure_service.extract(pdf_tmp)
    except Exception as exc:
        return [TextContent(type="text", text=f"ERROR extracting structure: {exc}")]

    if not structure.field_widgets:
        return [TextContent(
            type="text",
            text="No AcroForm fields found. This pipeline requires a PDF with interactive AcroForm fields.",
        )]

    alias_map = _alias_registry.assign(structure.field_widgets)

    try:
        _annotator.annotate(pdf_tmp, alias_map, annotated_tmp)
        all_page_images = _render_pages(annotated_tmp, dpi=dpi)
    except Exception as exc:
        return [TextContent(type="text", text=f"ERROR during annotation/rendering: {exc}")]
    finally:
        if _own_tmp:
            pdf_tmp.unlink(missing_ok=True)
        annotated_tmp.unlink(missing_ok=True)

    total_pages = len(all_page_images)
    # Clamp page range; default page_end caps at 5 pages per call to avoid context overflow
    _page_end = int(page_end_arg) if page_end_arg is not None else min(page_start + 4, total_pages)
    _page_end = min(_page_end, total_pages)
    page_start = min(page_start, total_pages)
    selected = all_page_images[page_start - 1 : _page_end]

    alias_map_dict = alias_map.to_dict()
    field_count = len(alias_map.alias_to_field)

    more_pages = _page_end < total_pages
    content: list[TextContent | ImageContent] = [
        TextContent(
            type="text",
            text=(
                f"Form analysis ready. Fields: {field_count}, Total pages: {total_pages}. "
                f"Showing pages {page_start}–{_page_end}."
                + (f" Call again with page_start={_page_end + 1} for the remaining pages." if more_pages else "")
                + "\n\nEach orange label (F001, F002, …) marks an AcroForm field. "
                "Identify what each field collects, then call save_field_mapping.\n\n"
                f"alias_map:\n{json.dumps(alias_map_dict, indent=2)}"
            ),
        ),
    ]

    for i, image_b64 in enumerate(selected):
        pg = page_start + i
        content.append(TextContent(type="text", text=f"--- Page {pg} of {total_pages} ---"))
        content.append(ImageContent(type="image", data=image_b64, mimeType="image/jpeg"))

    return content


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

    alias_index: dict[str, str] = alias_map_raw.get("alias_index") or {}

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

    schema = CanonicalSchema(
        form_family=form_family,
        version=version,
        mode="acroform",
        fields=fields,
    )

    schema_json = json.dumps(schema.to_dict(), indent=2)
    fill_script = schema.to_fill_script()

    return [TextContent(
        type="text",
        text=(
            f"Schema ({len(fields)} fields):\n```json\n{schema_json}\n```\n\n"
            f"{'─' * 60}\n\n{fill_script}"
        ),
    )]


# ---------------------------------------------------------------------------
# Page rendering helper
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
# Plain ASGI callable — no Starlette routing needed.
# Each request creates a fresh StreamableHTTPSessionManager so there is
# no shared state across invocations (required for Vercel serverless).

class _App:
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        # Per MCP streamable-HTTP spec: respond 405 to GET so clients fall back
        # to POST-only mode (required for serverless — no persistent SSE).
        if scope.get("method") == "GET":
            await send({
                "type": "http.response.start",
                "status": 405,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"allow", b"POST, DELETE"),
                ],
            })
            await send({"type": "http.response.body", "body": b""})
            return
        mgr = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
        async with mgr.run():
            await mgr.handle_request(scope, receive, send)


app = _App()
