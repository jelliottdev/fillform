"""Ingestion service: produces DocumentPackage metadata from a PDF file."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from .contracts import DocumentFingerprint, DocumentPackage, IngestDiagnostics


class PdfIngestError(Exception):
    """Base class for ingestion errors."""


class UnsupportedPdfError(PdfIngestError):
    """Raised when a file is not a supported or parseable PDF."""


class EncryptedPdfError(PdfIngestError):
    """Raised when an encrypted PDF is encountered."""


class CorruptPdfError(PdfIngestError):
    """Raised when the PDF appears malformed or truncated."""


@dataclass(slots=True)
class _ParsedPdf:
    parser: str
    page_count: int
    is_encrypted: bool
    has_acroform: bool
    native_text_pages: list[int]
    pdf_header: str | None
    trailer_id: list[str]
    info_keys: list[str]
    warnings: list[str]


class IngestionService:
    """Ingest PDF files and return robust parser-derived metadata."""

    def ingest(self, pdf_path: str | Path, document_id: str) -> DocumentPackage:
        path = Path(pdf_path)
        content = path.read_bytes()

        if not content.startswith(b"%PDF-"):
            raise UnsupportedPdfError(f"Unsupported file type at {path}: missing PDF header.")

        file_hash = hashlib.sha256(content).hexdigest()

        parsed = self._parse_pdf(content)
        if parsed.is_encrypted:
            raise EncryptedPdfError(f"PDF at {path} is encrypted and cannot be ingested.")

        has_native_text = bool(parsed.native_text_pages)
        has_form_fields = parsed.has_acroform
        pdf_type = "acroform" if has_form_fields else ("digital" if has_native_text else "scanned")

        diagnostics = IngestDiagnostics(
            parser=parsed.parser,
            page_count=parsed.page_count,
            is_encrypted=parsed.is_encrypted,
            has_acroform=has_form_fields,
            has_native_text=has_native_text,
            native_text_pages=parsed.native_text_pages,
            fingerprint=DocumentFingerprint(
                sha256=file_hash,
                file_size_bytes=len(content),
                parser=parsed.parser,
                pdf_header=parsed.pdf_header,
                trailer_id=parsed.trailer_id,
                info_keys=parsed.info_keys,
            ),
            warnings=parsed.warnings,
        )

        return DocumentPackage(
            document_id=document_id,
            file_hash=file_hash,
            page_count=parsed.page_count,
            pdf_type=pdf_type,
            has_native_text=has_native_text,
            has_form_fields=has_form_fields,
            diagnostics=diagnostics,
        )

    def _parse_pdf(self, content: bytes) -> _ParsedPdf:
        parser_errors: list[str] = []

        try:
            return self._parse_with_pypdf(content)
        except EncryptedPdfError:
            raise
        except Exception as exc:
            parser_errors.append(f"pypdf: {exc}")

        try:
            return self._parse_with_pymupdf(content)
        except EncryptedPdfError:
            raise
        except Exception as exc:
            parser_errors.append(f"pymupdf: {exc}")

        details = "; ".join(parser_errors) if parser_errors else "no parser could parse the file"
        raise CorruptPdfError(f"Unable to parse PDF bytes: {details}")

    def _parse_with_pypdf(self, content: bytes) -> _ParsedPdf:
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError
        except Exception as exc:
            raise UnsupportedPdfError(f"pypdf is unavailable: {exc}") from exc

        try:
            reader = PdfReader(BytesIO(content), strict=False)
            is_encrypted = bool(reader.is_encrypted)
            if is_encrypted:
                raise EncryptedPdfError("PDF is encrypted.")

            page_count = len(reader.pages)
            native_text_pages: list[int] = []
            for index, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                if text.strip():
                    native_text_pages.append(index)

            trailer = reader.trailer or {}
            root = trailer.get("/Root") if hasattr(trailer, "get") else None
            acroform = root.get("/AcroForm") if hasattr(root, "get") else None
            has_acroform = acroform is not None

            trailer_ids = trailer.get("/ID") if hasattr(trailer, "get") else None
            trailer_id_list = [str(item) for item in (trailer_ids or [])]

            metadata = reader.metadata or {}
            info_keys = sorted(str(key) for key in metadata.keys())

            pdf_header = getattr(reader, "pdf_header", None)
            if isinstance(pdf_header, bytes):
                pdf_header = pdf_header.decode("latin-1", errors="replace")

            return _ParsedPdf(
                parser="pypdf",
                page_count=page_count,
                is_encrypted=is_encrypted,
                has_acroform=has_acroform,
                native_text_pages=native_text_pages,
                pdf_header=str(pdf_header) if pdf_header is not None else None,
                trailer_id=trailer_id_list,
                info_keys=info_keys,
                warnings=[],
            )
        except EncryptedPdfError:
            raise
        except PdfReadError as exc:
            raise CorruptPdfError(f"pypdf failed to read PDF: {exc}") from exc
        except Exception as exc:
            raise CorruptPdfError(f"pypdf parser error: {exc}") from exc

    def _parse_with_pymupdf(self, content: bytes) -> _ParsedPdf:
        try:
            import fitz
        except Exception as exc:
            raise UnsupportedPdfError(f"PyMuPDF is unavailable: {exc}") from exc

        try:
            doc = fitz.open(stream=content, filetype="pdf")
            with doc:
                is_encrypted = bool(getattr(doc, "is_encrypted", False))
                needs_pass = bool(getattr(doc, "needs_pass", False))
                if is_encrypted or needs_pass:
                    raise EncryptedPdfError("PDF is encrypted.")

                page_count = int(doc.page_count)
                native_text_pages: list[int] = []
                for page_number in range(page_count):
                    page = doc.load_page(page_number)
                    if page.get_text("text").strip():
                        native_text_pages.append(page_number + 1)

                has_acroform = bool(getattr(doc, "is_form_pdf", False))
                metadata = getattr(doc, "metadata", {}) or {}
                info_keys = sorted(str(key) for key, value in metadata.items() if value)

                return _ParsedPdf(
                    parser="pymupdf",
                    page_count=page_count,
                    is_encrypted=False,
                    has_acroform=has_acroform,
                    native_text_pages=native_text_pages,
                    pdf_header=None,
                    trailer_id=[],
                    info_keys=info_keys,
                    warnings=[],
                )
        except EncryptedPdfError:
            raise
        except Exception as exc:
            raise CorruptPdfError(f"PyMuPDF parser error: {exc}") from exc
