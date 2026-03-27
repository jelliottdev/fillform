"""Cross-form packet validation for multi-form bankruptcy filings.

A bankruptcy case is not one form — it is a coordinated packet of 10–20 forms
that must be internally consistent.  This module defines:

  PacketSchema     — which forms are required for a given chapter, and the
                     cross-form consistency rules between them.

  FilledForm       — a single filled form within the packet (schema + payload
                     + fill result).

  FormPacket       — the full set of filled forms for one matter.

  PacketValidator  — validates consistency across all forms in a packet:
                     - Debtor identity matches everywhere
                     - Case number matches everywhere
                     - Totals from one form match input lines in another
                     - No required form is missing
                     - Cross-form arithmetic constraints

  PacketReport     — structured validation output, Markdown-renderable.

Chapter 7 required forms (official)
------------------------------------
  B-101   Voluntary Petition for Individuals Filing for Bankruptcy
  B-106A  Schedule A/B: Property
  B-106C  Schedule C: Property You Claim as Exempt
  B-106D  Schedule D: Creditors Who Have Claims Secured by Property
  B-106E  Schedule E/F: Creditors Who Have Unsecured Claims
  B-106G  Schedule G: Executory Contracts and Unexpired Leases
  B-106H  Schedule H: Your Codebtors
  B-106I  Schedule I: Your Income
  B-106J  Schedule J: Your Expenses
  B-108   Statement of Intention for Individuals Filing Under Chapter 7
  B-107   Statement of Financial Affairs for Individuals Filing for Bankruptcy
  B-2030  Disclosure of Compensation of Attorney for Debtor
  B-122A-1  Chapter 7 Statement of Your Current Monthly Income
  B-122A-2  Chapter 7 Means Test Calculation (if income above median)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .arithmetic import ArithmeticValidator
from .contracts import (
    CanonicalField,
    CanonicalSchema,
    FillPayload,
    ValidationIssue,
    VerificationCheck,
)
from .fill_engine import FillResult


# ---------------------------------------------------------------------------
# Packet schema — which forms a chapter requires
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FormRequirement:
    """Declaration of one form within a packet schema."""

    form_family: str
    label: str                       # Human-readable (e.g. "Schedule I: Income")
    required: bool = True
    condition: str | None = None     # Human-readable condition (e.g. "if income above median")

    def to_dict(self) -> dict[str, Any]:
        return {
            "form_family": self.form_family,
            "label": self.label,
            "required": self.required,
            "condition": self.condition,
        }


@dataclass(frozen=True)
class CrossFormRule:
    """A consistency rule between two fields in different forms.

    Examples
    --------
    - Debtor name on B-101 must match B-106I
    - Schedule I net income must equal B-122A-1 current monthly income
    - Case number must match everywhere
    """

    description: str
    source_family: str          # Form containing the authoritative value
    source_alias: str           # Alias or field_name in source form
    target_family: str          # Form that must match
    target_alias: str           # Alias or field_name in target form
    rule: str = "equals"        # "equals" | "sum_equals" | "numeric_equals"
    tolerance: float = 0.01     # For numeric comparisons

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "source_family": self.source_family,
            "source_alias": self.source_alias,
            "target_family": self.target_family,
            "target_alias": self.target_alias,
            "rule": self.rule,
            "tolerance": self.tolerance,
        }


@dataclass(frozen=True)
class PacketSchema:
    """Defines the required forms and cross-form rules for a bankruptcy chapter."""

    chapter: str                                # "7" | "13" | "11"
    name: str
    form_requirements: list[FormRequirement] = field(default_factory=list)
    cross_form_rules: list[CrossFormRule] = field(default_factory=list)

    @property
    def required_families(self) -> list[str]:
        return [f.form_family for f in self.form_requirements if f.required]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter": self.chapter,
            "name": self.name,
            "form_requirements": [f.to_dict() for f in self.form_requirements],
            "cross_form_rules": [r.to_dict() for r in self.cross_form_rules],
        }


# ---------------------------------------------------------------------------
# Built-in packet schemas
# ---------------------------------------------------------------------------

CHAPTER_7_PACKET = PacketSchema(
    chapter="7",
    name="Chapter 7 Individual Bankruptcy Packet",
    form_requirements=[
        FormRequirement("B-101", "Voluntary Petition", required=True),
        FormRequirement("B-106A", "Schedule A/B: Property", required=True),
        FormRequirement("B-106C", "Schedule C: Exemptions", required=True),
        FormRequirement("B-106D", "Schedule D: Secured Creditors", required=True),
        FormRequirement("B-106E", "Schedule E/F: Unsecured Creditors", required=True),
        FormRequirement("B-106G", "Schedule G: Executory Contracts", required=True),
        FormRequirement("B-106H", "Schedule H: Codebtors", required=True),
        FormRequirement("B-106I", "Schedule I: Income", required=True),
        FormRequirement("B-106J", "Schedule J: Expenses", required=True),
        FormRequirement("B-107", "Statement of Financial Affairs", required=True),
        FormRequirement("B-108", "Statement of Intention", required=True),
        FormRequirement("B-122A-1", "Chapter 7 Current Monthly Income", required=True),
        FormRequirement("B-122A-2", "Chapter 7 Means Test Calculation",
                        required=False,
                        condition="Required if current monthly income exceeds state median"),
        FormRequirement("B-2030", "Attorney Compensation Disclosure", required=True),
    ],
    cross_form_rules=[
        CrossFormRule(
            description="Debtor name must match across petition and all schedules",
            source_family="B-101",
            source_alias="debtor_name",
            target_family="B-106I",
            target_alias="debtor_name",
            rule="equals",
        ),
        CrossFormRule(
            description="Schedule I net income should equal means test current monthly income",
            source_family="B-106I",
            source_alias="net_monthly_income",
            target_family="B-122A-1",
            target_alias="current_monthly_income",
            rule="numeric_equals",
            tolerance=1.00,  # Allow $1 rounding difference
        ),
    ],
)

CHAPTER_13_PACKET = PacketSchema(
    chapter="13",
    name="Chapter 13 Individual Bankruptcy Packet",
    form_requirements=[
        FormRequirement("B-101", "Voluntary Petition", required=True),
        FormRequirement("B-106A", "Schedule A/B: Property", required=True),
        FormRequirement("B-106C", "Schedule C: Exemptions", required=True),
        FormRequirement("B-106D", "Schedule D: Secured Creditors", required=True),
        FormRequirement("B-106E", "Schedule E/F: Unsecured Creditors", required=True),
        FormRequirement("B-106G", "Schedule G: Executory Contracts", required=True),
        FormRequirement("B-106H", "Schedule H: Codebtors", required=True),
        FormRequirement("B-106I", "Schedule I: Income", required=True),
        FormRequirement("B-106J", "Schedule J: Expenses", required=True),
        FormRequirement("B-107", "Statement of Financial Affairs", required=True),
        FormRequirement("B-122C-1", "Chapter 13 Current Monthly Income", required=True),
        FormRequirement("B-122C-2", "Chapter 13 Disposable Income",
                        required=False,
                        condition="Required if above-median income"),
    ],
    cross_form_rules=[
        CrossFormRule(
            description="Schedule I net income must match Chapter 13 current monthly income",
            source_family="B-106I",
            source_alias="net_monthly_income",
            target_family="B-122C-1",
            target_alias="current_monthly_income",
            rule="numeric_equals",
            tolerance=1.00,
        ),
    ],
)

_PACKET_SCHEMAS: dict[str, PacketSchema] = {
    "7": CHAPTER_7_PACKET,
    "13": CHAPTER_13_PACKET,
}


def get_packet_schema(chapter: str) -> PacketSchema | None:
    return _PACKET_SCHEMAS.get(str(chapter))


# ---------------------------------------------------------------------------
# Filled form — one form within a packet
# ---------------------------------------------------------------------------

@dataclass
class FilledForm:
    """A single filled form within a packet."""

    form_family: str
    schema: CanonicalSchema
    payload: FillPayload
    fill_result: FillResult | None = None
    output_path: str | None = None

    def get_value(self, alias_or_name: str) -> Any:
        """Retrieve the payload value for an alias or field_name."""
        v = self.payload.values.get(alias_or_name)
        if v is not None:
            return v
        # Try alias lookup via schema
        for f in self.schema.fields:
            if f.alias == alias_or_name or f.field_name == alias_or_name:
                return self.payload.values.get(f.alias) or self.payload.values.get(f.field_name)
        return None


# ---------------------------------------------------------------------------
# Form packet
# ---------------------------------------------------------------------------

@dataclass
class FormPacket:
    """The complete set of filled forms for one matter."""

    matter_id: str
    chapter: str
    forms: list[FilledForm] = field(default_factory=list)
    packet_schema: PacketSchema | None = None

    def __post_init__(self) -> None:
        if self.packet_schema is None:
            self.packet_schema = get_packet_schema(self.chapter)

    def add_form(self, form: FilledForm) -> None:
        self.forms.append(form)

    def get_form(self, form_family: str) -> FilledForm | None:
        return next((f for f in self.forms if f.form_family == form_family), None)

    @property
    def present_families(self) -> set[str]:
        return {f.form_family for f in self.forms}

    @property
    def missing_required_families(self) -> list[str]:
        if self.packet_schema is None:
            return []
        return [
            ff for ff in self.packet_schema.required_families
            if ff not in self.present_families
        ]


# ---------------------------------------------------------------------------
# Packet issue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PacketIssue:
    """A validation issue at the packet (cross-form) level."""

    kind: str           # "missing_form" | "identity_mismatch" | "cross_form_arithmetic" | "consistency"
    severity: str       # "error" | "warning"
    description: str
    source_family: str | None = None
    target_family: str | None = None
    source_alias: str | None = None
    target_alias: str | None = None
    source_value: Any = None
    target_value: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "description": self.description,
            "source_family": self.source_family,
            "target_family": self.target_family,
            "source_alias": self.source_alias,
            "target_alias": self.target_alias,
            "source_value": self.source_value,
            "target_value": self.target_value,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Packet report
# ---------------------------------------------------------------------------

@dataclass
class PacketReport:
    """Aggregate cross-form validation result."""

    matter_id: str
    chapter: str
    forms_present: list[str]
    forms_missing: list[str]
    issues: list[PacketIssue] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def errors(self) -> list[PacketIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[PacketIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def is_complete(self) -> bool:
        return not self.forms_missing and not self.errors

    def summary(self) -> str:
        lines = [
            f"Packet Report — Chapter {self.chapter} — Matter {self.matter_id}",
            f"Forms present : {len(self.forms_present)} | Missing: {len(self.forms_missing)}",
            f"Issues        : {len(self.errors)} error(s), {len(self.warnings)} warning(s)",
            f"Packet ready  : {'YES' if self.is_complete else 'NO'}",
        ]
        if self.forms_missing:
            lines.append(f"Missing forms : {', '.join(self.forms_missing)}")
        for issue in self.issues:
            icon = "✗" if issue.severity == "error" else "⚠"
            lines.append(f"  {icon} [{issue.kind}] {issue.description}")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        lines = [
            f"# Packet Validation Report",
            f"**Matter:** {self.matter_id}  |  **Chapter:** {self.chapter}  |  "
            f"**Generated:** {self.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
            "",
        ]
        if self.is_complete:
            lines.append("> **PACKET COMPLETE** — No blocking errors found.")
        else:
            lines.append(
                f"> **PACKET INCOMPLETE** — "
                f"{len(self.forms_missing)} form(s) missing, "
                f"{len(self.errors)} error(s)."
            )
        lines += [
            "",
            "## Form Checklist",
            "",
            "| Form | Status |",
            "|------|--------|",
        ]
        for ff in sorted(self.forms_present):
            lines.append(f"| {ff} | ✓ Present |")
        for ff in sorted(self.forms_missing):
            lines.append(f"| {ff} | ✗ **Missing** |")
        lines.append("")

        if self.errors:
            lines += ["## ⛔ Errors", ""]
            for issue in self.errors:
                lines.append(f"**[{issue.kind}]** {issue.description}")
                if issue.source_family and issue.source_value is not None:
                    lines.append(
                        f"- {issue.source_family}/{issue.source_alias}: `{issue.source_value}`"
                    )
                if issue.target_family and issue.target_value is not None:
                    lines.append(
                        f"- {issue.target_family}/{issue.target_alias}: `{issue.target_value}`"
                    )
                lines.append("")

        if self.warnings:
            lines += ["## ⚠ Warnings", ""]
            for issue in self.warnings:
                lines.append(f"**[{issue.kind}]** {issue.description}")
                lines.append("")

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matter_id": self.matter_id,
            "chapter": self.chapter,
            "generated_at": self.generated_at.isoformat(),
            "is_complete": self.is_complete,
            "forms_present": sorted(self.forms_present),
            "forms_missing": sorted(self.forms_missing),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [i.to_dict() for i in self.issues],
        }


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class PacketValidator:
    """Validates consistency across all forms in a :class:`FormPacket`."""

    def validate(self, packet: FormPacket) -> PacketReport:
        """Run all packet-level checks.

        Checks
        ------
        1. Required forms present
        2. Debtor identity consistent across forms (name, SSN)
        3. Case number consistent across forms
        4. Cross-form arithmetic rules from the packet schema
        5. Any custom CrossFormRule instances
        """
        issues: list[PacketIssue] = []

        # ── 1. Form completeness ──────────────────────────────────────
        missing = packet.missing_required_families

        # ── 2 & 3. Identity and case number consistency ───────────────
        issues.extend(self._check_identity_consistency(packet))

        # ── 4 & 5. Cross-form rules ───────────────────────────────────
        if packet.packet_schema:
            for rule in packet.packet_schema.cross_form_rules:
                issue = self._evaluate_cross_form_rule(packet, rule)
                if issue:
                    issues.append(issue)

        return PacketReport(
            matter_id=packet.matter_id,
            chapter=packet.chapter,
            forms_present=sorted(packet.present_families),
            forms_missing=sorted(missing),
            issues=issues,
        )

    # ------------------------------------------------------------------
    # Identity checks
    # ------------------------------------------------------------------

    def _check_identity_consistency(self, packet: FormPacket) -> list[PacketIssue]:
        """Verify that debtor name, SSN, and case number match across all forms."""
        issues: list[PacketIssue] = []

        # Collect values for identity fields across all forms
        identity_fields = {
            "debtor_name": [],
            "case_number": [],
            "ssn": [],
        }

        for filled_form in packet.forms:
            for canonical in filled_form.schema.fields:
                label_lower = (canonical.label or "").lower()
                field_lower = canonical.field_name.lower()

                # Debtor name
                if any(tok in label_lower or tok in field_lower
                       for tok in ("debtor name", "debtor's name", "full name")):
                    v = filled_form.get_value(canonical.alias)
                    if v:
                        identity_fields["debtor_name"].append(
                            (filled_form.form_family, canonical.alias, str(v).strip())
                        )

                # Case number
                if any(tok in label_lower or tok in field_lower
                       for tok in ("case number", "case no", "bankruptcy case")):
                    v = filled_form.get_value(canonical.alias)
                    if v:
                        identity_fields["case_number"].append(
                            (filled_form.form_family, canonical.alias, str(v).strip())
                        )

                # SSN / ITIN
                if any(tok in label_lower or tok in field_lower
                       for tok in ("social security", "ssn", "itin", "taxpayer id")):
                    v = filled_form.get_value(canonical.alias)
                    if v:
                        identity_fields["ssn"].append(
                            (filled_form.form_family, canonical.alias, str(v).strip())
                        )

        # Check each identity field for consistency
        for field_kind, entries in identity_fields.items():
            if len(entries) < 2:
                continue
            reference_family, reference_alias, reference_value = entries[0]
            for form_family, alias, value in entries[1:]:
                if _normalise_for_compare(value) != _normalise_for_compare(reference_value):
                    issues.append(PacketIssue(
                        kind="identity_mismatch",
                        severity="error",
                        description=(
                            f"{field_kind.replace('_', ' ').title()} mismatch: "
                            f"'{reference_value}' on {reference_family} vs "
                            f"'{value}' on {form_family}."
                        ),
                        source_family=reference_family,
                        source_alias=reference_alias,
                        source_value=reference_value,
                        target_family=form_family,
                        target_alias=alias,
                        target_value=value,
                    ))

        return issues

    # ------------------------------------------------------------------
    # Cross-form rule evaluation
    # ------------------------------------------------------------------

    def _evaluate_cross_form_rule(
        self,
        packet: FormPacket,
        rule: CrossFormRule,
    ) -> PacketIssue | None:
        source_form = packet.get_form(rule.source_family)
        target_form = packet.get_form(rule.target_family)

        if source_form is None or target_form is None:
            return None  # Missing form — caught by completeness check

        source_val = source_form.get_value(rule.source_alias)
        target_val = target_form.get_value(rule.target_alias)

        if source_val is None or target_val is None:
            return None  # Value missing — caught by per-form verification

        if rule.rule == "equals":
            source_str = _normalise_for_compare(str(source_val))
            target_str = _normalise_for_compare(str(target_val))
            if source_str != target_str:
                return PacketIssue(
                    kind="consistency",
                    severity="error",
                    description=rule.description + f": '{source_val}' ≠ '{target_val}'.",
                    source_family=rule.source_family,
                    source_alias=rule.source_alias,
                    source_value=source_val,
                    target_family=rule.target_family,
                    target_alias=rule.target_alias,
                    target_value=target_val,
                )

        elif rule.rule in {"numeric_equals", "sum_equals"}:
            try:
                sv = float(str(source_val).replace(",", "").replace("$", ""))
                tv = float(str(target_val).replace(",", "").replace("$", ""))
                if abs(sv - tv) > rule.tolerance:
                    return PacketIssue(
                        kind="cross_form_arithmetic",
                        severity="error",
                        description=(
                            rule.description
                            + f": {rule.source_family} shows {sv:.2f}, "
                            f"{rule.target_family} shows {tv:.2f} "
                            f"(off by {tv - sv:+.2f})."
                        ),
                        source_family=rule.source_family,
                        source_alias=rule.source_alias,
                        source_value=sv,
                        target_family=rule.target_family,
                        target_alias=rule.target_alias,
                        target_value=tv,
                        metadata={"delta": round(tv - sv, 4), "tolerance": rule.tolerance},
                    )
            except ValueError:
                return None

        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_for_compare(value: str) -> str:
    """Normalise a string for identity comparison (lowercase, strip, collapse whitespace)."""
    import re
    return re.sub(r"\s+", " ", value.lower().strip())
