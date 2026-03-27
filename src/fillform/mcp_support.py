from __future__ import annotations

import base64
import binascii
import hashlib
import tempfile
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse
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


def create_session(pdf_path: Path, alias_map: dict[str, str], pdf_fingerprint: str | None = None) -> str:
    session_id = str(uuid.uuid4())
    _analysis_sessions[session_id] = {
        "pdf_path": str(pdf_path),
        "alias_map": dict(alias_map),
        "pdf_fingerprint": pdf_fingerprint,
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
    return _normalize_pdf_path(str(candidate))


def _normalize_pdf_path(candidate: str) -> Path:
    raw = candidate.strip()
    if not raw:
        raise ValueError("pdf_path was empty.")

    # Handle URL-like wrappers that appear in some MCP runtimes.
    if raw.startswith("sandbox:"):
        raw = raw.removeprefix("sandbox:")
    elif raw.startswith("file://"):
        parsed = urlparse(raw)
        raw = unquote(parsed.path or "")

    path = Path(raw).expanduser().resolve()
    if path.exists():
        return path

    # Common proxied-mount fallback: paths sometimes arrive as `/mnt/data/...`
    # even when the file is materialized into a local working directory.
    if raw.startswith("/mnt/data/"):
        fallback = Path(raw.removeprefix("/mnt/data/")).expanduser().resolve()
        if fallback.exists():
            return fallback

    return path


def compute_pdf_fingerprint(pdf_path: Path) -> str:
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
