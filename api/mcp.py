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
          "url": "https://<your-project>.vercel.app/mcp"
        }
      }
    }
"""

from __future__ import annotations

import base64
import contextlib
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
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

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
                "the alias key-mapping JSON plus a rendered PNG image of each annotated "
                "page. Accepts the PDF as a base64-encoded string. Use the images to "
                "identify what each labeled field collects, then call save_field_mapping."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pdf_base64": {
                        "type": "string",
                        "description": "Base64-encoded PDF file content.",
                    },
                    "dpi": {
                        "type": "integer",
                        "description": "Rendering resolution for page images (default 150).",
                        "default": 150,
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
    pdf_b64 = args.get("pdf_base64") or ""
    dpi = int(args.get("dpi") or 150)

    if not pdf_b64:
        return [TextContent(type="text", text="ERROR: pdf_base64 is required.")]

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception as exc:
        return [TextContent(type="text", text=f"ERROR decoding pdf_base64: {exc}")]

    # Write PDF to /tmp for processing
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir="/tmp") as f:
        f.write(pdf_bytes)
        pdf_tmp = Path(f.name)

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
        page_images = _render_pages(annotated_tmp, dpi=dpi)
    except Exception as exc:
        return [TextContent(type="text", text=f"ERROR during annotation/rendering: {exc}")]
    finally:
        pdf_tmp.unlink(missing_ok=True)
        annotated_tmp.unlink(missing_ok=True)

    alias_map_dict = alias_map.to_dict()
    field_count = len(alias_map.alias_to_field)
    page_count = len(page_images)

    content: list[TextContent | ImageContent] = [
        TextContent(
            type="text",
            text=(
                f"Form analysis ready. Fields: {field_count}, Pages: {page_count}.\n\n"
                f"Each orange label (F001, F002, …) marks an AcroForm field. "
                f"Identify what each field collects, then call save_field_mapping.\n\n"
                f"alias_map:\n{json.dumps(alias_map_dict, indent=2)}"
            ),
        ),
    ]

    for page_index, image_b64 in enumerate(page_images):
        content.append(TextContent(type="text", text=f"--- Page {page_index + 1} of {page_count} ---"))
        content.append(ImageContent(type="image", data=image_b64, mimeType="image/png"))

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

def _render_pages(pdf_path: Path, dpi: int = 150) -> list[str]:
    import fitz
    images: list[str] = []
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    with fitz.open(str(pdf_path)) as doc:
        for i in range(doc.page_count):
            pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
            images.append(base64.standard_b64encode(pix.tobytes("png")).decode())
    return images


# ---------------------------------------------------------------------------
# Starlette ASGI app  (Vercel detects the `app` variable)
# ---------------------------------------------------------------------------

session_manager = StreamableHTTPSessionManager(app=server, stateless=True)


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette):
    async with session_manager.run():
        yield


async def _handle_mcp(scope, receive, send) -> None:
    await session_manager.handle_request(scope, receive, send)


app = Starlette(
    routes=[Route("/mcp", endpoint=_handle_mcp, methods=["GET", "POST", "DELETE"])],
    lifespan=_lifespan,
)
