"""Tool-facing API surface for agents (MCP-oriented skeleton)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contracts import CanonicalSchema, FillPayload, VerificationReport
from .fill_engine import FillEngine, FillResult
from .ingest import IngestionService
from .schema_registry import SchemaRegistry
from .verify import VerificationEngine


@dataclass(slots=True)
class FillFormService:
    ingestion: IngestionService
    registry: SchemaRegistry
    fill_engine: FillEngine
    verification: VerificationEngine

    def upload_form(self, pdf_path: str | Path, document_id: str):
        return self.ingestion.ingest(pdf_path, document_id)

    def get_schema(self, form_family: str, version: str) -> CanonicalSchema | None:
        return self.registry.get(form_family, version)

    def fill_form(
        self,
        source_pdf: str | Path,
        schema: CanonicalSchema,
        payload: FillPayload,
    ) -> FillResult:
        return self.fill_engine.fill(source_pdf, schema, payload)

    def verify_form(self, payload: FillPayload) -> VerificationReport:
        return self.verification.verify(payload)
