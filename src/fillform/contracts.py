"""Stable contracts for fillform verification and execution history.

These dataclasses are intentionally explicit about serialization via ``to_dict`` /
``from_dict`` to keep API boundaries and persisted history deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

# PDF classification: AcroForm interactive, native digital text, or scanned image.
PdfType = Literal["acroform", "digital", "scanned"]


@dataclass(frozen=True)
class CanonicalField:
    """Semantic description of a single form field, enriched by vision analysis."""

    alias: str                          # FXXX sequential identifier (e.g. F001)
    field_name: str                     # Raw PDF AcroForm field name
    field_type: str                     # Tx / Btn / Ch / Sig / unknown
    page: int                           # 0-based page index
    bbox: tuple[float, float, float, float]  # (x0, y0, x1, y1) in PDF units

    label: str | None = None            # Human-readable label inferred from context
    context: str | None = None          # What information the field collects
    expected_value_type: str | None = None  # string | date | number | boolean | signature | selection
    expected_format: str | None = None  # e.g. "MM/DD/YYYY", "XXX-XX-XXXX"
    is_required: bool = False
    section: str | None = None          # Form section / group name

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "field_name": self.field_name,
            "field_type": self.field_type,
            "page": self.page,
            "bbox": list(self.bbox),
            "label": self.label,
            "context": self.context,
            "expected_value_type": self.expected_value_type,
            "expected_format": self.expected_format,
            "is_required": self.is_required,
            "section": self.section,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalField":
        bbox_raw = payload["bbox"]
        return cls(
            alias=str(payload["alias"]),
            field_name=str(payload["field_name"]),
            field_type=str(payload["field_type"]),
            page=int(payload["page"]),
            bbox=(float(bbox_raw[0]), float(bbox_raw[1]), float(bbox_raw[2]), float(bbox_raw[3])),
            label=payload.get("label"),
            context=payload.get("context"),
            expected_value_type=payload.get("expected_value_type"),
            expected_format=payload.get("expected_format"),
            is_required=bool(payload.get("is_required", False)),
            section=payload.get("section"),
        )


@dataclass(frozen=True)
class CanonicalSchema:
    """Complete semantic mapping of all fields in a PDF form."""

    form_family: str
    version: str
    mode: str                           # acroform | overlay | hybrid
    fields: list[CanonicalField] = field(default_factory=list)

    @property
    def alias_map(self) -> dict[str, str]:
        """Return {field_name: alias} lookup."""
        return {f.field_name: f.alias for f in self.fields}

    def to_dict(self) -> dict[str, Any]:
        return {
            "form_family": self.form_family,
            "version": self.version,
            "mode": self.mode,
            "fields": [f.to_dict() for f in self.fields],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalSchema":
        return cls(
            form_family=str(payload["form_family"]),
            version=str(payload["version"]),
            mode=str(payload["mode"]),
            fields=[CanonicalField.from_dict(f) for f in payload.get("fields", [])],
        )

    def to_fill_script(self) -> str:
        """Generate a plain-text fill guide that an AI agent can use to fill the form.

        The returned document describes every field, what data to collect from the
        user, and the expected format, giving an LLM everything it needs to
        deterministically fill the form once it has gathered the required information.
        """
        lines: list[str] = [
            f"# Form Fill Guide: {self.form_family} (version {self.version})",
            f"## Mode: {self.mode}  |  Total fields: {len(self.fields)}",
            "",
            "---",
            "",
            "## Field Definitions",
            "",
        ]

        for f in self.fields:
            heading = f.label or f.field_name
            lines.append(f"### {f.alias} — {heading}")
            lines.append(f"- **PDF field name**: `{f.field_name}`")
            lines.append(f"- **Field type**: {f.field_type}")
            if f.section:
                lines.append(f"- **Section**: {f.section}")
            if f.context:
                lines.append(f"- **Purpose**: {f.context}")
            if f.expected_value_type:
                lines.append(f"- **Expected value type**: {f.expected_value_type}")
            if f.expected_format:
                lines.append(f"- **Format**: `{f.expected_format}`")
            lines.append(f"- **Required**: {'Yes' if f.is_required else 'No'}")
            lines.append(f"- **Page**: {f.page + 1}")
            lines.append("")

        lines += [
            "---",
            "",
            "## AI Filler Instructions",
            "",
            "To fill this form, collect the following information from the user,",
            "then call the fill API with the alias → value mapping.",
            "",
            "### Required fields",
            "",
        ]

        required = [f for f in self.fields if f.is_required]
        optional = [f for f in self.fields if not f.is_required]

        if required:
            for f in required:
                label = f.label or f.field_name
                vtype = f.expected_value_type or "text"
                fmt = f"  (format: `{f.expected_format}`)" if f.expected_format else ""
                lines.append(f"- **{f.alias}** — {label}: {vtype}{fmt}")
        else:
            lines.append("*(none marked required)*")

        lines += ["", "### Optional fields", ""]

        if optional:
            for f in optional:
                label = f.label or f.field_name
                vtype = f.expected_value_type or "text"
                fmt = f"  (format: `{f.expected_format}`)" if f.expected_format else ""
                lines.append(f"- **{f.alias}** — {label}: {vtype}{fmt}")
        else:
            lines.append("*(none)*")

        lines += [
            "",
            "---",
            "",
            "## Alias → Field Name Key",
            "",
            "```json",
        ]
        mapping = {f.alias: f.field_name for f in self.fields}
        import json
        lines.append(json.dumps(mapping, indent=2))
        lines += ["```", ""]

        return "\n".join(lines)


@dataclass(frozen=True)
class FillPayload:
    """Data provided by the user to fill a specific form instance."""

    schema_family: str
    schema_version: str
    # Values keyed by alias (F001) or raw field name — fill engine resolves both.
    values: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_family": self.schema_family,
            "schema_version": self.schema_version,
            "values": dict(self.values),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FillPayload":
        return cls(
            schema_family=str(payload["schema_family"]),
            schema_version=str(payload["schema_version"]),
            values=dict(payload.get("values") or {}),
        )


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class EvidenceItem:
    """Evidence used to support a verification or validation outcome."""

    source_type: str
    snippet: str | None = None
    reference: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "snippet": self.snippet,
            "reference": self.reference,
            "score": self.score,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceItem":
        return cls(
            source_type=str(payload["source_type"]),
            snippet=payload.get("snippet"),
            reference=payload.get("reference"),
            score=float(payload["score"]) if payload.get("score") is not None else None,
            metadata=dict(payload.get("metadata") or {}),
        )

@dataclass(slots=True)
class DocumentFingerprint:
    sha256: str
    file_size_bytes: int
    parser: str
    pdf_header: str | None = None
    trailer_id: list[str] = field(default_factory=list)
    info_keys: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IngestDiagnostics:
    parser: str
    page_count: int
    is_encrypted: bool
    has_acroform: bool
    has_native_text: bool
    native_text_pages: list[int]
    fingerprint: DocumentFingerprint
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DocumentPackage:
    document_id: str
    file_hash: str
    page_count: int
    pdf_type: PdfType
    has_native_text: bool
    has_form_fields: bool
    diagnostics: IngestDiagnostics

@dataclass(frozen=True)
class ValidationIssue:
    """Structured validation issue produced while checking field constraints."""

    field: str
    rule: str
    severity: str
    message: str
    code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "code": self.code,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ValidationIssue":
        return cls(
            field=str(payload["field"]),
            rule=str(payload["rule"]),
            severity=str(payload["severity"]),
            message=str(payload["message"]),
            code=payload.get("code"),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class ArtifactRef:
    """Reference to an artifact generated or consulted during processing."""

    kind: str
    path: str | None = None
    uri: str | None = None
    checksum: str | None = None
    checksum_algorithm: str = "sha256"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "uri": self.uri,
            "checksum": self.checksum,
            "checksum_algorithm": self.checksum_algorithm,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            kind=str(payload["kind"]),
            path=payload.get("path"),
            uri=payload.get("uri"),
            checksum=payload.get("checksum"),
            checksum_algorithm=str(payload.get("checksum_algorithm") or "sha256"),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class VerificationCheck:
    """Per-check verification outcome with metadata and categorized failures."""

    check_id: str
    status: str
    category: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidenceItem] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "category": self.category,
            "message": self.message,
            "metadata": dict(self.metadata),
            "evidence": [item.to_dict() for item in self.evidence],
            "issues": [item.to_dict() for item in self.issues],
            "artifacts": [item.to_dict() for item in self.artifacts],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VerificationCheck":
        return cls(
            check_id=str(payload["check_id"]),
            status=str(payload["status"]),
            category=payload.get("category"),
            message=payload.get("message"),
            metadata=dict(payload.get("metadata") or {}),
            evidence=[EvidenceItem.from_dict(item) for item in payload.get("evidence", [])],
            issues=[ValidationIssue.from_dict(item) for item in payload.get("issues", [])],
            artifacts=[ArtifactRef.from_dict(item) for item in payload.get("artifacts", [])],
        )


@dataclass(frozen=True)
class VerificationReport:
    """Overall verification report containing per-check metadata and failures."""

    verified: bool
    checks: list[VerificationCheck] = field(default_factory=list)
    failure_categories: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    generated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        categories = dict(self.failure_categories)
        if not categories:
            for check in self.checks:
                if check.status.lower() in {"failed", "error"}:
                    category = check.category or "uncategorized"
                    categories[category] = categories.get(category, 0) + 1

        return {
            "verified": self.verified,
            "checks": [check.to_dict() for check in self.checks],
            "failure_categories": categories,
            "metadata": dict(self.metadata),
            "generated_at": _format_datetime(self.generated_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VerificationReport":
        return cls(
            verified=bool(payload["verified"]),
            checks=[VerificationCheck.from_dict(item) for item in payload.get("checks", [])],
            failure_categories={
                str(key): int(value) for key, value in dict(payload.get("failure_categories") or {}).items()
            },
            metadata=dict(payload.get("metadata") or {}),
            generated_at=_parse_datetime(payload.get("generated_at")),
        )


@dataclass(frozen=True)
class FillWriteAction:
    """Deterministic write action emitted during form fill operations."""

    sequence: int
    action: str
    target: str
    payload_checksum: str | None = None
    before_checksum: str | None = None
    after_checksum: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "action": self.action,
            "target": self.target,
            "payload_checksum": self.payload_checksum,
            "before_checksum": self.before_checksum,
            "after_checksum": self.after_checksum,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FillWriteAction":
        return cls(
            sequence=int(payload["sequence"]),
            action=str(payload["action"]),
            target=str(payload["target"]),
            payload_checksum=payload.get("payload_checksum"),
            before_checksum=payload.get("before_checksum"),
            after_checksum=payload.get("after_checksum"),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class FillLogEntry:
    """Single, serializable log record for a form fill execution step."""

    entry_id: str
    event: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    write_actions: list[FillWriteAction] = field(default_factory=list)
    verification_report: VerificationReport | None = None
    artifacts: list[ArtifactRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ordered_actions = sorted(self.write_actions, key=lambda action: action.sequence)
        return {
            "entry_id": self.entry_id,
            "event": self.event,
            "created_at": _format_datetime(self.created_at),
            "started_at": _format_datetime(self.started_at),
            "completed_at": _format_datetime(self.completed_at),
            "write_actions": [action.to_dict() for action in ordered_actions],
            "verification_report": self.verification_report.to_dict() if self.verification_report else None,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FillLogEntry":
        return cls(
            entry_id=str(payload["entry_id"]),
            event=str(payload["event"]),
            created_at=_parse_datetime(str(payload["created_at"])) or datetime.now(timezone.utc),
            started_at=_parse_datetime(payload.get("started_at")),
            completed_at=_parse_datetime(payload.get("completed_at")),
            write_actions=[FillWriteAction.from_dict(item) for item in payload.get("write_actions", [])],
            verification_report=(
                VerificationReport.from_dict(payload["verification_report"])
                if payload.get("verification_report")
                else None
            ),
            artifacts=[ArtifactRef.from_dict(item) for item in payload.get("artifacts", [])],
            metadata=dict(payload.get("metadata") or {}),
        )
