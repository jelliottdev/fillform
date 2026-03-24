"""Core data contracts for the FillForm architecture."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


PdfType = Literal["acroform", "xfa", "digital", "scanned"]
FillMode = Literal["acroform", "overlay", "hybrid", "portal"]
VerificationStatus = Literal["pass", "fail", "abstain"]


@dataclass(slots=True)
class BoundingBox:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(slots=True)
class DocumentPackage:
    document_id: str
    file_hash: str
    page_count: int
    pdf_type: PdfType
    has_native_text: bool
    has_form_fields: bool


@dataclass(slots=True)
class RawField:
    raw_name: str
    field_type: str
    bbox: BoundingBox


@dataclass(slots=True)
class StructuralPage:
    page: int
    width: float
    height: float
    fields: list[RawField] = field(default_factory=list)
    text_blocks: list[str] = field(default_factory=list)
    lines: list[BoundingBox] = field(default_factory=list)
    boxes: list[BoundingBox] = field(default_factory=list)


@dataclass(slots=True)
class StructuralRepresentation:
    pages: list[StructuralPage]


@dataclass(slots=True)
class CanonicalField:
    field_id: str
    canonical_name: str
    raw_name: str
    field_type: str
    page: int
    bbox: BoundingBox
    required: bool
    confidence: float
    evidence: list[str] = field(default_factory=list)
    validators: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CanonicalSchema:
    form_family: str
    version: str
    mode: FillMode
    fields: list[CanonicalField]


@dataclass(slots=True)
class FillPayload:
    values: dict[str, Any]


@dataclass(slots=True)
class CheckedField:
    canonical_name: str
    expected: str
    observed: str
    match: bool


@dataclass(slots=True)
class VerificationReport:
    status: VerificationStatus
    score: float
    issues: list[str] = field(default_factory=list)
    checked_fields: list[CheckedField] = field(default_factory=list)
