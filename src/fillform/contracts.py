"""Stable contracts for fillform verification and execution history.

These dataclasses are intentionally explicit about serialization via ``to_dict`` /
``from_dict`` to keep API boundaries and persisted history deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


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
