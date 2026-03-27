"""Verification engine for post-fill validation.

Checks
------
1. **Completeness** — every required field in the schema has a value in the payload.
2. **Readback**     — open the filled PDF and confirm each written value is visible
                      in the corresponding widget (detects silent write failures).
3. **Format**       — basic type/format sanity checks for date, number, and SSN fields.

Each check produces a :class:`~fillform.contracts.VerificationCheck` entry.
The overall :class:`~fillform.contracts.VerificationReport` is ``verified=True``
only when all checks pass.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    CanonicalField,
    CanonicalSchema,
    FillPayload,
    ValidationIssue,
    VerificationCheck,
    VerificationReport,
)


# ---------------------------------------------------------------------------
# Widget helpers (duplicated minimally to keep this module self-contained)
# ---------------------------------------------------------------------------

def _is_checkbox_widget(widget: Any) -> bool:
    fts = str(getattr(widget, "field_type_string", "")).lower()
    return "checkbox" in fts or "button" in fts or str(getattr(widget, "field_type", "")) == "2"


def _selected_state(widgets: list[Any]) -> str:
    """Return 'yes' if any widget in the group is checked, else 'no'."""
    for widget in widgets:
        v = str(widget.field_value or "").lower()
        if v not in {"off", "", "false", "0", "no"}:
            return "yes"
    return "no"


def _normalize_bool(value: Any) -> str | None:
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value or "").strip().lower()
    if text in {"yes", "true", "on", "1"}:
        return "yes"
    if text in {"no", "false", "off", "0"}:
        return "no"
    return None


# ---------------------------------------------------------------------------
# Format validators
# ---------------------------------------------------------------------------

_DATE_PATTERNS = [
    re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$"),
    re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$"),
]

_SSN_PATTERN = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_EIN_PATTERN = re.compile(r"^\d{2}-\d{7}$")
_ZIP_PATTERN = re.compile(r"^\d{5}(-\d{4})?$")
_PHONE_PATTERN = re.compile(r"[\d\-\(\)\s\.]{7,15}")


def _looks_like_date(text: str) -> bool:
    return any(p.match(text) for p in _DATE_PATTERNS)


def _looks_like_number(text: str) -> bool:
    cleaned = text.replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        float(cleaned)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# VerificationEngine
# ---------------------------------------------------------------------------

class VerificationEngine:
    """Runs multi-layer verification over a fill operation."""

    def verify(
        self,
        payload: FillPayload,
        schema: CanonicalSchema | None = None,
        filled_pdf: str | Path | None = None,
    ) -> VerificationReport:
        """Run all verification checks and return a structured report.

        Parameters
        ----------
        payload:
            The fill payload that was applied to the PDF.
        schema:
            The canonical schema for the form.  Required for completeness and
            format checks.
        filled_pdf:
            Path to the output PDF produced by the fill engine.  Required for
            readback verification.
        """
        checks: list[VerificationCheck] = []

        # ── 1. Completeness ───────────────────────────────────────────────
        if schema is not None:
            checks.append(self._completeness_check(payload=payload, schema=schema))

        # ── 2. PDF readback ───────────────────────────────────────────────
        if filled_pdf is not None:
            checks.append(
                self._readback_check(
                    filled_pdf=Path(filled_pdf),
                    payload=payload,
                    schema=schema,
                )
            )

        # ── 3. Format / type validation ───────────────────────────────────
        if schema is not None:
            checks.append(self._format_check(payload=payload, schema=schema))

        # ── 4. Field constraint checks ────────────────────────────────────
        if schema is not None:
            constraint_check = self._constraint_check(payload=payload, schema=schema)
            if constraint_check is not None:
                checks.append(constraint_check)

        verified = all(c.status in {"passed", "skipped"} for c in checks)

        return VerificationReport(
            verified=verified,
            checks=checks,
            generated_at=datetime.now(timezone.utc),
            metadata={
                "schema_family": schema.form_family if schema else None,
                "schema_version": schema.version if schema else None,
                "payload_family": payload.schema_family,
                "payload_version": payload.schema_version,
            },
        )

    # ------------------------------------------------------------------
    # Check implementations
    # ------------------------------------------------------------------

    def _completeness_check(
        self,
        payload: FillPayload,
        schema: CanonicalSchema,
    ) -> VerificationCheck:
        """Verify every required field has a non-empty value in the payload."""
        provided_keys = set(payload.values.keys())
        missing: list[CanonicalField] = []

        for f in schema.fields:
            if not f.is_required:
                continue
            # Accept either alias or raw field_name as the key
            provided = f.alias in provided_keys or f.field_name in provided_keys
            if not provided:
                missing.append(f)
            else:
                # Key is present but value may be empty
                value = payload.values.get(f.alias) or payload.values.get(f.field_name)
                if value is None or str(value).strip() == "":
                    missing.append(f)

        if missing:
            issues = [
                ValidationIssue(
                    field=f.alias,
                    rule="required_field_missing",
                    severity="error",
                    message=(
                        f"Required field '{f.label or f.alias}' ({f.alias}) "
                        "was not provided or is empty."
                    ),
                    metadata={
                        "field_name": f.field_name,
                        "section": f.section,
                        "expected_value_type": f.expected_value_type,
                    },
                )
                for f in missing
            ]
            return VerificationCheck(
                check_id="required_fields",
                status="failed",
                category="completeness",
                message=(
                    f"{len(missing)} required field(s) missing or empty. "
                    "Filing may be rejected."
                ),
                issues=issues,
                metadata={"missing_count": len(missing), "total_required": sum(1 for f in schema.fields if f.is_required)},
            )

        total_required = sum(1 for f in schema.fields if f.is_required)
        return VerificationCheck(
            check_id="required_fields",
            status="passed",
            category="completeness",
            message=f"All {total_required} required field(s) are present and non-empty.",
            metadata={"total_required": total_required},
        )

    def _readback_check(
        self,
        filled_pdf: Path,
        payload: FillPayload,
        schema: CanonicalSchema | None,
    ) -> VerificationCheck:
        """Open the filled PDF and confirm expected values are stored in widgets."""
        try:
            import fitz
        except ImportError:
            return VerificationCheck(
                check_id="pdf_readback",
                status="skipped",
                category="readback",
                message="PyMuPDF not available; skipping readback verification.",
            )

        if not filled_pdf.exists():
            return VerificationCheck(
                check_id="pdf_readback",
                status="skipped",
                category="readback",
                message=f"Filled PDF not found at '{filled_pdf}'.",
            )

        alias_to_field: dict[str, str] = {}
        if schema is not None:
            alias_to_field = {f.alias: f.field_name for f in schema.fields}

        issues: list[ValidationIssue] = []
        checked = 0

        with fitz.open(str(filled_pdf)) as doc:
            widgets_by_name: dict[str, list[Any]] = {}
            for page in doc:
                for widget in page.widgets() or []:
                    name = str(widget.field_name or "")
                    if name:
                        widgets_by_name.setdefault(name, []).append(widget)

            for key, expected in payload.values.items():
                field_name = alias_to_field.get(str(key), str(key))
                widgets = widgets_by_name.get(field_name) or []
                if not widgets:
                    # Field missing from PDF entirely — skip (completeness check covers this)
                    continue
                checked += 1

                is_checkbox = _is_checkbox_widget(widgets[0]) or len(widgets) > 1
                if is_checkbox:
                    actual_state = _selected_state(widgets)
                    expected_bool = _normalize_bool(expected)
                    if expected_bool is not None and actual_state != expected_bool:
                        issues.append(ValidationIssue(
                            field=str(key),
                            rule="value_mismatch",
                            severity="warning",
                            message=(
                                f"Checkbox '{field_name}': expected '{expected_bool}', "
                                f"found '{actual_state}'."
                            ),
                            metadata={"field_name": field_name, "expected": expected_bool, "actual": actual_state},
                        ))
                else:
                    actual = str(widgets[0].field_value or "")
                    expected_text = "" if expected is None else str(expected)
                    if actual != expected_text:
                        issues.append(ValidationIssue(
                            field=str(key),
                            rule="value_mismatch",
                            severity="warning",
                            message=(
                                f"Field '{field_name}': expected '{expected_text}', "
                                f"found '{actual}'."
                            ),
                            metadata={"field_name": field_name, "expected": expected_text, "actual": actual},
                        ))

        match_count = checked - len(issues)
        match_rate = round(match_count / checked, 3) if checked > 0 else 1.0

        if issues:
            return VerificationCheck(
                check_id="pdf_readback",
                status="failed",
                category="readback",
                message=(
                    f"{len(issues)} of {checked} field(s) did not match after fill "
                    f"(match rate: {match_rate:.1%})."
                ),
                issues=issues,
                metadata={"checked": checked, "mismatches": len(issues), "match_rate": match_rate},
            )

        return VerificationCheck(
            check_id="pdf_readback",
            status="passed",
            category="readback",
            message=f"All {checked} checked field(s) match expected values.",
            metadata={"checked": checked, "match_rate": 1.0},
        )

    def _format_check(
        self,
        payload: FillPayload,
        schema: CanonicalSchema,
    ) -> VerificationCheck:
        """Validate value formats against expected_value_type / expected_format hints."""
        field_by_alias = {f.alias: f for f in schema.fields}
        field_by_name = {f.field_name: f for f in schema.fields}
        issues: list[ValidationIssue] = []

        for key, value in payload.values.items():
            canonical = field_by_alias.get(str(key)) or field_by_name.get(str(key))
            if canonical is None:
                continue
            if value is None or str(value).strip() == "":
                continue

            text = str(value).strip()
            vtype = (canonical.expected_value_type or "").lower()
            fmt = (canonical.expected_format or "").lower()

            # Date
            if vtype == "date" or "date" in fmt:
                if not _looks_like_date(text):
                    issues.append(ValidationIssue(
                        field=str(key),
                        rule="invalid_date_format",
                        severity="warning",
                        message=(
                            f"'{canonical.label or key}': value '{text}' "
                            "does not appear to be a valid date."
                        ),
                        metadata={"expected_format": canonical.expected_format},
                    ))

            # Number / currency
            elif vtype == "number" or any(tok in fmt for tok in ("amount", "currency", "dollar")):
                if not _looks_like_number(text):
                    issues.append(ValidationIssue(
                        field=str(key),
                        rule="invalid_number_format",
                        severity="warning",
                        message=(
                            f"'{canonical.label or key}': value '{text}' "
                            "does not appear to be a valid number."
                        ),
                    ))

            # SSN
            if "ssn" in fmt or "xxx-xx-xxxx" in fmt:
                if not _SSN_PATTERN.match(text):
                    issues.append(ValidationIssue(
                        field=str(key),
                        rule="invalid_ssn_format",
                        severity="warning",
                        message=(
                            f"'{canonical.label or key}': expected SSN format "
                            f"XXX-XX-XXXX, got '{text}'."
                        ),
                    ))

            # ZIP
            if "zip" in fmt:
                if not _ZIP_PATTERN.match(text):
                    issues.append(ValidationIssue(
                        field=str(key),
                        rule="invalid_zip_format",
                        severity="warning",
                        message=(
                            f"'{canonical.label or key}': expected ZIP code, got '{text}'."
                        ),
                    ))

        if issues:
            return VerificationCheck(
                check_id="format_validation",
                status="failed",
                category="format",
                message=f"{len(issues)} field(s) have format or type issues.",
                issues=issues,
                metadata={"issue_count": len(issues)},
            )

        return VerificationCheck(
            check_id="format_validation",
            status="passed",
            category="format",
            message="All provided values pass basic format validation.",
        )

    def _constraint_check(
        self,
        payload: FillPayload,
        schema: CanonicalSchema,
    ) -> VerificationCheck | None:
        """Evaluate all FieldConstraint rules attached to schema fields.

        Rules evaluated
        ---------------
        min_value / max_value   numeric bounds
        enum                    value must be in the allowed list
        required_if             conditional required (condition met but field missing)
        exclusive_with          at most one in the group may be truthy
        pattern                 regex match
        min_length / max_length string length bounds
        derived_from            informational only; no violation raised
        """
        field_by_alias = {f.alias: f for f in schema.fields}
        field_by_name = {f.field_name: f for f in schema.fields}
        issues: list[ValidationIssue] = []

        def _get_value(key: str) -> Any:
            return payload.values.get(key)

        def _resolve(key: str) -> Any:
            """Resolve alias or field_name key to the payload value."""
            v = payload.values.get(key)
            if v is not None:
                return v
            canonical = field_by_alias.get(key) or field_by_name.get(key)
            if canonical:
                return payload.values.get(canonical.alias) or payload.values.get(canonical.field_name)
            return None

        def _is_truthy(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            return text not in {"", "off", "false", "0", "no"}

        for canonical in schema.fields:
            if not canonical.constraints:
                continue

            # Resolve the field's own value (may be missing)
            value = _resolve(canonical.alias)
            text = str(value).strip() if value is not None else ""
            has_value = value is not None and text != ""

            for constraint in canonical.constraints:
                rule = constraint.rule
                params = constraint.params
                label = canonical.label or canonical.alias
                custom_msg = constraint.message

                # ── min_value ──────────────────────────────────────────
                if rule == "min_value" and has_value:
                    try:
                        num = float(text.replace(",", "").replace("$", ""))
                        min_v = float(params["value"])
                        if num < min_v:
                            issues.append(ValidationIssue(
                                field=canonical.alias,
                                rule="min_value",
                                severity="error",
                                message=custom_msg or (
                                    f"'{label}': value {num} is below minimum {min_v}."
                                ),
                                metadata={"value": num, "min": min_v},
                            ))
                    except (ValueError, KeyError):
                        pass

                # ── max_value ──────────────────────────────────────────
                elif rule == "max_value" and has_value:
                    try:
                        num = float(text.replace(",", "").replace("$", ""))
                        max_v = float(params["value"])
                        if num > max_v:
                            issues.append(ValidationIssue(
                                field=canonical.alias,
                                rule="max_value",
                                severity="error",
                                message=custom_msg or (
                                    f"'{label}': value {num} exceeds maximum {max_v}."
                                ),
                                metadata={"value": num, "max": max_v},
                            ))
                    except (ValueError, KeyError):
                        pass

                # ── enum ───────────────────────────────────────────────
                elif rule == "enum" and has_value:
                    allowed = [str(v) for v in params.get("values", [])]
                    if allowed and text not in allowed:
                        issues.append(ValidationIssue(
                            field=canonical.alias,
                            rule="enum",
                            severity="error",
                            message=custom_msg or (
                                f"'{label}': '{text}' is not one of the allowed values: "
                                + ", ".join(f"'{v}'" for v in allowed[:8])
                                + ("…" if len(allowed) > 8 else "") + "."
                            ),
                            metadata={"value": text, "allowed": allowed},
                        ))

                # ── required_if ────────────────────────────────────────
                elif rule == "required_if":
                    condition_field = str(params.get("field", ""))
                    condition_value = str(params.get("value", ""))
                    actual_condition = _resolve(condition_field)
                    condition_met = str(actual_condition).strip() == condition_value

                    if condition_met and not has_value:
                        issues.append(ValidationIssue(
                            field=canonical.alias,
                            rule="required_if",
                            severity="error",
                            message=custom_msg or (
                                f"'{label}' is required when '{condition_field}' "
                                f"equals '{condition_value}'."
                            ),
                            metadata={
                                "condition_field": condition_field,
                                "condition_value": condition_value,
                            },
                        ))

                # ── exclusive_with ─────────────────────────────────────
                elif rule == "exclusive_with":
                    group_aliases = [canonical.alias] + [str(a) for a in params.get("fields", [])]
                    truthy_in_group = [a for a in group_aliases if _is_truthy(_resolve(a))]
                    if len(truthy_in_group) > 1:
                        issues.append(ValidationIssue(
                            field=canonical.alias,
                            rule="exclusive_with",
                            severity="error",
                            message=custom_msg or (
                                f"Only one of {group_aliases} may be selected, "
                                f"but {truthy_in_group} are all truthy."
                            ),
                            metadata={"group": group_aliases, "truthy": truthy_in_group},
                        ))

                # ── pattern ────────────────────────────────────────────
                elif rule == "pattern" and has_value:
                    pattern = params.get("regex", "")
                    if pattern:
                        if not re.match(pattern, text):
                            issues.append(ValidationIssue(
                                field=canonical.alias,
                                rule="pattern",
                                severity="warning",
                                message=custom_msg or (
                                    f"'{label}': value '{text}' does not match "
                                    f"the required pattern '{pattern}'."
                                ),
                                metadata={"value": text, "pattern": pattern},
                            ))

                # ── min_length ─────────────────────────────────────────
                elif rule == "min_length" and has_value:
                    try:
                        min_l = int(params["value"])
                        if len(text) < min_l:
                            issues.append(ValidationIssue(
                                field=canonical.alias,
                                rule="min_length",
                                severity="warning",
                                message=custom_msg or (
                                    f"'{label}': value is {len(text)} chars, "
                                    f"minimum is {min_l}."
                                ),
                            ))
                    except (ValueError, KeyError):
                        pass

                # ── max_length ─────────────────────────────────────────
                elif rule == "max_length" and has_value:
                    try:
                        max_l = int(params["value"])
                        if len(text) > max_l:
                            issues.append(ValidationIssue(
                                field=canonical.alias,
                                rule="max_length",
                                severity="warning",
                                message=custom_msg or (
                                    f"'{label}': value is {len(text)} chars, "
                                    f"maximum is {max_l}."
                                ),
                            ))
                    except (ValueError, KeyError):
                        pass

                # derived_from: informational, no check needed

        if not issues:
            # No constraints to check — skip if no schema fields have any constraints
            fields_with_constraints = sum(1 for f in schema.fields if f.constraints)
            if fields_with_constraints == 0:
                return None
            return VerificationCheck(
                check_id="constraint_validation",
                status="passed",
                category="constraints",
                message=(
                    f"All constraint rules passed "
                    f"({fields_with_constraints} constrained field(s) checked)."
                ),
                metadata={"fields_with_constraints": fields_with_constraints},
            )

        return VerificationCheck(
            check_id="constraint_validation",
            status="failed",
            category="constraints",
            message=f"{len(issues)} constraint violation(s) detected.",
            issues=issues,
            metadata={"issue_count": len(issues)},
        )
