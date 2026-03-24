"""FillForm architecture skeleton package."""

from .contracts import (
    CanonicalField,
    CanonicalSchema,
    DocumentFingerprint,
    DocumentPackage,
    FillPayload,
    IngestDiagnostics,
    VerificationReport,
)
from .fill_engine import FillEngine
from .ingest import (
    CorruptPdfError,
    EncryptedPdfError,
    IngestionService,
    UnsupportedPdfError,
)
from .mapper import SemanticMapper
from .mcp_server import FillFormService
from .schema_registry import SchemaRegistry
from .verify import VerificationEngine

__all__ = [
    "CanonicalField",
    "CanonicalSchema",
    "DocumentFingerprint",
    "DocumentPackage",
    "FillPayload",
    "IngestDiagnostics",
    "VerificationReport",
    "FillEngine",
    "IngestionService",
    "UnsupportedPdfError",
    "EncryptedPdfError",
    "CorruptPdfError",
    "SemanticMapper",
    "FillFormService",
    "SchemaRegistry",
    "VerificationEngine",
]
