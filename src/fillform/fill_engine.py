"""Deterministic fill engine interface and placeholder implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contracts import CanonicalSchema, FillPayload


@dataclass(slots=True)
class FillResult:
    draft_pdf_path: Path
    flattened_pdf_path: Path
    fill_log: dict[str, str]


class FillEngine:
    def fill(
        self,
        source_pdf: str | Path,
        schema: CanonicalSchema,
        payload: FillPayload,
    ) -> FillResult:
        """Fill according to the schema in deterministic mode.

        This stub copies source path pointers only; production code should write fields
        using AcroForm, overlay, hybrid, or portal strategies.
        """
        source = Path(source_pdf)
        return FillResult(
            draft_pdf_path=source,
            flattened_pdf_path=source,
            fill_log={"mode": schema.mode, "field_count": str(len(payload.values))},
        )
