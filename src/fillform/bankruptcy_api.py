"""HTTP API for syncing official US Courts bankruptcy forms."""

from __future__ import annotations

import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .bankruptcy_tool import BankruptcyFormsTool, BankruptcySyncRequest

DEFAULT_DATA_DIR = Path(os.environ.get("FILLFORM_BANKRUPTCY_DATA_DIR", "./.fillform_bankruptcy"))
DEFAULT_OUTPUT_DIR = DEFAULT_DATA_DIR / "forms"
DEFAULT_STATE_PATH = DEFAULT_DATA_DIR / "sync_state.json"


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "fillform-bankruptcy-sync"})


async def sync_bankruptcy_forms(request: Request) -> JSONResponse:
    body = {}
    if request.method == "POST":
        raw = await request.body()
        if raw:
            body = json.loads(raw.decode("utf-8"))

    try:
        sync_request = BankruptcySyncRequest.from_payload(
            body,
            default_output_dir=DEFAULT_OUTPUT_DIR,
            default_state_path=DEFAULT_STATE_PATH,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    tool = BankruptcyFormsTool()
    try:
        result = tool.run(sync_request)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    return JSONResponse({"ok": True, "result": result})


app = Starlette(
    debug=False,
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/bankruptcy-forms/sync", sync_bankruptcy_forms, methods=["POST"]),
    ],
)
