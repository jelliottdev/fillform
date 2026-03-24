"""FillForm architecture skeleton package."""

from .contracts import (
    CanonicalField,
    CanonicalSchema,
    DocumentPackage,
    FillPayload,
    VerificationReport,
)
from .fill_engine import FillEngine
from .ingest import IngestionService
from .mapper import SemanticMapper
from .mcp_server import FillFormService
from .schema_registry import SchemaRegistry
from .verify import VerificationEngine

__all__ = [
    "CanonicalField",
    "CanonicalSchema",
    "DocumentPackage",
    "FillPayload",
    "VerificationReport",
    "FillEngine",
    "IngestionService",
    "SemanticMapper",
    "FillFormService",
    "SchemaRegistry",
    "VerificationEngine",
]
