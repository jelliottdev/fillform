"""FillForm architecture skeleton package."""

from .annotator import PdfAnnotator
from .bankruptcy_forms import SyncResult, USCourtsBankruptcyFormsSync
from .bankruptcy_tool import BankruptcyFormsTool, BankruptcySyncRequest
from .contracts import (
    ArtifactRef,
    CanonicalField,
    CanonicalSchema,
    DocumentFingerprint,
    DocumentPackage,
    FillPayload,
    IngestDiagnostics,
    PdfType,
    VerificationReport,
)
from .field_alias import AliasMap, FieldAliasRegistry
from .fill_engine import FillEngine
from .ingest import (
    CorruptPdfError,
    EncryptedPdfError,
    IngestionService,
    UnsupportedPdfError,
)
from .mapper import SemanticMapper
from .mcp import server as fillform_mcp_server
from .mcp_server import FillFormService
from .schema_registry import SchemaRegistry
from .verify import VerificationEngine
from .vision_mapper import VisionFieldMapper

__all__ = [
    # Contracts
    "ArtifactRef",
    "CanonicalField",
    "CanonicalSchema",
    "DocumentFingerprint",
    "DocumentPackage",
    "FillPayload",
    "IngestDiagnostics",
    "PdfType",
    "VerificationReport",
    # Field alias mapping
    "AliasMap",
    "FieldAliasRegistry",
    # PDF annotation
    "PdfAnnotator",
    "USCourtsBankruptcyFormsSync",
    "SyncResult",
    "BankruptcyFormsTool",
    "BankruptcySyncRequest",
    # Vision analysis
    "VisionFieldMapper",
    # Core services
    "FillEngine",
    "IngestionService",
    "SemanticMapper",
    "FillFormService",
    "SchemaRegistry",
    "VerificationEngine",
    # MCP server (token-free, Claude-native analysis)
    "fillform_mcp_server",
    # Errors
    "UnsupportedPdfError",
    "EncryptedPdfError",
    "CorruptPdfError",
]
