"""Unified service layer for bankruptcy form sync operations.

This module centralizes request parsing/validation and execution so the same
logic can be used from HTTP handlers, CLI entry points, or direct library calls.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .bankruptcy_forms import USCourtsBankruptcyFormsSync


@dataclass(slots=True)
class BankruptcySyncRequest:
    output_dir: Path
    state_path: Path
    download_pdfs: bool = True
    min_request_interval_seconds: float = 1.2
    max_form_pages: int | None = None

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        default_output_dir: Path,
        default_state_path: Path,
    ) -> "BankruptcySyncRequest":
        output_dir = Path(payload.get("output_dir", default_output_dir))
        state_path = Path(payload.get("state_path", default_state_path))

        download_pdfs = bool(payload.get("download_pdfs", True))
        min_request_interval_seconds = float(payload.get("min_request_interval_seconds", 1.2))
        if min_request_interval_seconds <= 0:
            raise ValueError("min_request_interval_seconds must be > 0")

        max_form_pages_raw = payload.get("max_form_pages")
        max_form_pages: int | None = None
        if max_form_pages_raw is not None:
            max_form_pages = int(max_form_pages_raw)
            if max_form_pages < 0:
                raise ValueError("max_form_pages must be >= 0")

        return cls(
            output_dir=output_dir,
            state_path=state_path,
            download_pdfs=download_pdfs,
            min_request_interval_seconds=min_request_interval_seconds,
            max_form_pages=max_form_pages,
        )


class BankruptcyFormsTool:
    """Single orchestration entry point for bankruptcy form sync."""

    def run(self, request: BankruptcySyncRequest) -> dict[str, Any]:
        syncer = USCourtsBankruptcyFormsSync(min_request_interval_seconds=request.min_request_interval_seconds)
        result = syncer.sync(
            output_dir=request.output_dir,
            state_path=request.state_path,
            download_pdfs=request.download_pdfs,
            max_form_pages=request.max_form_pages,
        )
        return asdict(result)
