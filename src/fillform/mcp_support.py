from __future__ import annotations

import base64
import binascii
import tempfile
import uuid
from pathlib import Path
from typing import Any

PDF_BYTES_DESCRIPTION = (
    "Optional base64-encoded PDF bytes. Use this when file-path rewriting is "
    "unavailable in proxied mount environments."
)


def pdf_source_properties(path_description: str) -> dict[str, Any]:
    return {
        "pdf_path": {
            "type": "string",
            "description": path_description,
        },
        "pdf_bytes_base64": {
            "type": "string",
            "description": PDF_BYTES_DESCRIPTION,
        },
    }


_analysis_sessions: dict[str, dict[str, Any]] = {}


def create_session(pdf_path: Path, alias_map: dict[str, str]) -> str:
    session_id = str(uuid.uuid4())
    _analysis_sessions[session_id] = {
        "pdf_path": str(pdf_path),
        "alias_map": dict(alias_map),
    }
    if len(_analysis_sessions) > 100:
        for key in list(_analysis_sessions.keys())[:20]:
            _analysis_sessions.pop(key, None)
    return session_id


def get_session(session_id: Any) -> dict[str, Any] | None:
    if not session_id:
        return None
    return _analysis_sessions.get(str(session_id))


def resolve_pdf_source(args: dict[str, Any], default_path: str | None = None) -> Path:
    if args.get("pdf_bytes_base64"):
        b64_payload = str(args["pdf_bytes_base64"])
        try:
            payload = base64.b64decode(b64_payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"pdf_bytes_base64 is not valid base64: {exc}") from exc
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(payload)
        tmp.close()
        return Path(tmp.name).resolve()

    candidate = args.get("pdf_path") or default_path
    if not candidate:
        raise ValueError("Provide either pdf_path or pdf_bytes_base64.")
    return Path(str(candidate)).expanduser().resolve()
