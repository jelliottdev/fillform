"""Ingestion service: produces DocumentPackage metadata from a PDF file."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .contracts import DocumentPackage


class IngestionService:
    """Ingest PDF files and return lightweight metadata for downstream services."""

    def ingest(self, pdf_path: str | Path, document_id: str) -> DocumentPackage:
        path = Path(pdf_path)
        content = path.read_bytes()
        file_hash = hashlib.sha256(content).hexdigest()

        # Placeholder heuristics; replace with real PDF parsing.
        has_form_fields = b"/AcroForm" in content
        has_native_text = b"BT" in content
        pdf_type = "acroform" if has_form_fields else "digital"

        # Minimal page-count heuristic using page marker occurrences.
        page_count = max(content.count(b"/Type /Page"), 1)

        return DocumentPackage(
            document_id=document_id,
            file_hash=file_hash,
            page_count=page_count,
            pdf_type=pdf_type,
            has_native_text=has_native_text,
            has_form_fields=has_form_fields,
        )
