"""Cross-field arithmetic validation for bankruptcy form consistency.

Per-field format checks cannot detect calculation errors — a total can be a
perfectly valid number while still being wrong.  This module evaluates
arithmetic relationships across fields.

Typical bankruptcy use cases
----------------------------
- Schedule I net monthly income must equal income total minus deductions
- Schedule J total expenses must equal the sum of all expense line items
- Means test current monthly income must be internally consistent
- Dependent counts on the petition must match downstream schedule references
- Any field annotated as ``derived_from`` should match the computed result

Constraints are expressed on the **result field** (the one that should hold
the computed value) as ``FieldConstraint`` rules in ``CanonicalField``.

Supported arithmetic rules
--------------------------
``sum_of``
    ``{"fields": ["F010", "F011", "F012"], "tolerance": 0.01}``
    The field's value must equal ``sum(F010, F011, F012)`` within *tolerance*.

``diff_of``
    ``{"minuend": "F010", "subtrahend": "F011", "tolerance": 0.01}``
    The field's value must equal ``F010 − F011`` within *tolerance*.

``equals_field``
    ``{"field": "F020"}``
    The field's value must equal another field exactly (cross-form consistency).

``percent_of``
    ``{"field": "F010", "percent": 60, "tolerance": 0.50}``
    The field's value must be approximately *percent*% of another field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import CanonicalField, CanonicalSchema, FillPayload, ValidationIssue


# ---------------------------------------------------------------------------
# Arithmetic result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArithmeticCheckResult:
    """Result of a single arithmetic constraint check."""

    result_alias: str
    rule: str
    status: str           # "passed" | "failed" | "skipped"
    expected: float | None = None
    actual: float | None = None
    delta: float | None = None
    tolerance: float | None = None
    message: str | None = None
    operand_aliases: list[str] = field(default_factory=list)

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_alias": self.result_alias,
            "rule": self.rule,
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
            "delta": self.delta,
            "tolerance": self.tolerance,
            "message": self.message,
            "operand_aliases": list(self.operand_aliases),
        }


@dataclass
class ArithmeticReport:
    """Aggregate arithmetic validation result."""

    checks: list[ArithmeticCheckResult] = field(default_factory=list)

    @property
    def passed(self) -> list[ArithmeticCheckResult]:
        return [c for c in self.checks if c.status == "passed"]

    @property
    def failed(self) -> list[ArithmeticCheckResult]:
        return [c for c in self.checks if c.status == "failed"]

    @property
    def skipped(self) -> list[ArithmeticCheckResult]:
        return [c for c in self.checks if c.status == "skipped"]

    @property
    def is_valid(self) -> bool:
        return len(self.failed) == 0

    def as_validation_issues(self) -> list[ValidationIssue]:
        """Convert arithmetic failures to ValidationIssue instances."""
        issues: list[ValidationIssue] = []
        for c in self.failed:
            issues.append(ValidationIssue(
                field=c.result_alias,
                rule=c.rule,
                severity="error",
                message=c.message or (
                    f"Arithmetic check failed: expected {c.expected}, "
                    f"got {c.actual} (delta {c.delta:+.4f})."
                ),
                metadata={
                    "expected": c.expected,
                    "actual": c.actual,
                    "delta": c.delta,
                    "tolerance": c.tolerance,
                    "operands": c.operand_aliases,
                },
            ))
        return issues

    def summary(self) -> str:
        if not self.checks:
            return "No arithmetic constraints defined."
        lines = [
            f"Arithmetic: {len(self.passed)} passed, "
            f"{len(self.failed)} failed, {len(self.skipped)} skipped."
        ]
        for c in self.failed:
            lines.append(
                f"  [FAIL] {c.result_alias} ({c.rule}): "
                f"expected {c.expected}, got {c.actual} (Δ={c.delta:+.4f})"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "total": len(self.checks),
            "passed": len(self.passed),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class ArithmeticValidator:
    """Evaluates arithmetic FieldConstraint rules across a fill payload."""

    def validate(
        self,
        payload: FillPayload,
        schema: CanonicalSchema,
    ) -> ArithmeticReport:
        """Check all arithmetic constraints in *schema* against *payload*.

        Returns an :class:`ArithmeticReport` describing every check, whether
        it passed, failed, or was skipped (missing operand values).
        """
        field_by_alias = {f.alias: f for f in schema.fields}
        field_by_name = {f.field_name: f for f in schema.fields}
        report = ArithmeticReport()

        for canonical in schema.fields:
            for constraint in canonical.constraints:
                if constraint.rule not in {"sum_of", "diff_of", "equals_field", "percent_of"}:
                    continue
                result = self._evaluate(
                    result_field=canonical,
                    constraint_rule=constraint.rule,
                    params=constraint.params,
                    custom_msg=constraint.message,
                    payload=payload,
                    field_by_alias=field_by_alias,
                    field_by_name=field_by_name,
                )
                report.checks.append(result)

        return report

    # ------------------------------------------------------------------
    # Rule evaluators
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        result_field: CanonicalField,
        constraint_rule: str,
        params: dict[str, Any],
        custom_msg: str | None,
        payload: FillPayload,
        field_by_alias: dict[str, CanonicalField],
        field_by_name: dict[str, CanonicalField],
    ) -> ArithmeticCheckResult:
        alias = result_field.alias
        label = result_field.label or alias
        tol = float(params.get("tolerance", 0.01))

        def resolve_num(key: str) -> float | None:
            """Get a numeric value from the payload by alias or field_name."""
            raw = payload.values.get(key)
            if raw is None:
                # Try reverse lookup via alias map
                canonical = field_by_alias.get(key) or field_by_name.get(key)
                if canonical:
                    raw = (payload.values.get(canonical.alias)
                           or payload.values.get(canonical.field_name))
            if raw is None:
                return None
            try:
                return float(str(raw).replace(",", "").replace("$", "").strip())
            except ValueError:
                return None

        # ── sum_of ────────────────────────────────────────────────────
        if constraint_rule == "sum_of":
            operand_keys: list[str] = [str(k) for k in params.get("fields", [])]
            result_val = resolve_num(alias)

            operand_vals: list[float] = []
            missing_keys: list[str] = []
            for key in operand_keys:
                v = resolve_num(key)
                if v is None:
                    missing_keys.append(key)
                else:
                    operand_vals.append(v)

            if result_val is None:
                return ArithmeticCheckResult(
                    result_alias=alias, rule="sum_of", status="skipped",
                    message=f"'{label}' has no value; cannot verify sum.",
                    operand_aliases=operand_keys,
                )
            if missing_keys:
                return ArithmeticCheckResult(
                    result_alias=alias, rule="sum_of", status="skipped",
                    message=(
                        f"Missing operand(s) {missing_keys} for sum check on '{label}'. "
                        "Fill those fields first."
                    ),
                    operand_aliases=operand_keys,
                )

            expected = sum(operand_vals)
            delta = abs(result_val - expected)
            ok = delta <= tol

            return ArithmeticCheckResult(
                result_alias=alias, rule="sum_of",
                status="passed" if ok else "failed",
                expected=round(expected, 4),
                actual=round(result_val, 4),
                delta=round(result_val - expected, 4),
                tolerance=tol,
                operand_aliases=operand_keys,
                message=None if ok else (custom_msg or (
                    f"'{label}': expected sum {expected:.2f} "
                    f"({' + '.join(str(round(v, 2)) for v in operand_vals)}), "
                    f"got {result_val:.2f} (off by {result_val - expected:+.2f})."
                )),
            )

        # ── diff_of ───────────────────────────────────────────────────
        elif constraint_rule == "diff_of":
            minuend_key = str(params.get("minuend", ""))
            subtrahend_key = str(params.get("subtrahend", ""))
            result_val = resolve_num(alias)
            minuend = resolve_num(minuend_key)
            subtrahend = resolve_num(subtrahend_key)

            if result_val is None:
                return ArithmeticCheckResult(
                    result_alias=alias, rule="diff_of", status="skipped",
                    message=f"'{label}' has no value; cannot verify difference.",
                    operand_aliases=[minuend_key, subtrahend_key],
                )
            if minuend is None or subtrahend is None:
                return ArithmeticCheckResult(
                    result_alias=alias, rule="diff_of", status="skipped",
                    message=(
                        f"Operand(s) missing for diff check on '{label}'. "
                        f"Need '{minuend_key}' and '{subtrahend_key}'."
                    ),
                    operand_aliases=[minuend_key, subtrahend_key],
                )

            expected = minuend - subtrahend
            delta = abs(result_val - expected)
            ok = delta <= tol

            return ArithmeticCheckResult(
                result_alias=alias, rule="diff_of",
                status="passed" if ok else "failed",
                expected=round(expected, 4),
                actual=round(result_val, 4),
                delta=round(result_val - expected, 4),
                tolerance=tol,
                operand_aliases=[minuend_key, subtrahend_key],
                message=None if ok else (custom_msg or (
                    f"'{label}': expected {minuend:.2f} − {subtrahend:.2f} = {expected:.2f}, "
                    f"got {result_val:.2f} (off by {result_val - expected:+.2f})."
                )),
            )

        # ── equals_field ──────────────────────────────────────────────
        elif constraint_rule == "equals_field":
            other_key = str(params.get("field", ""))
            result_val = resolve_num(alias)
            other_val = resolve_num(other_key)

            if result_val is None or other_val is None:
                return ArithmeticCheckResult(
                    result_alias=alias, rule="equals_field", status="skipped",
                    message=f"One or both fields ('{alias}', '{other_key}') have no numeric value.",
                    operand_aliases=[other_key],
                )

            delta = abs(result_val - other_val)
            ok = delta <= tol

            return ArithmeticCheckResult(
                result_alias=alias, rule="equals_field",
                status="passed" if ok else "failed",
                expected=round(other_val, 4),
                actual=round(result_val, 4),
                delta=round(result_val - other_val, 4),
                tolerance=tol,
                operand_aliases=[other_key],
                message=None if ok else (custom_msg or (
                    f"'{label}' ({result_val:.2f}) must equal '{other_key}' "
                    f"({other_val:.2f}), off by {result_val - other_val:+.2f}."
                )),
            )

        # ── percent_of ────────────────────────────────────────────────
        elif constraint_rule == "percent_of":
            base_key = str(params.get("field", ""))
            pct = float(params.get("percent", 0))
            result_val = resolve_num(alias)
            base_val = resolve_num(base_key)

            if result_val is None or base_val is None:
                return ArithmeticCheckResult(
                    result_alias=alias, rule="percent_of", status="skipped",
                    message=f"One or both fields ('{alias}', '{base_key}') have no numeric value.",
                    operand_aliases=[base_key],
                )

            expected = base_val * pct / 100.0
            delta = abs(result_val - expected)
            ok = delta <= tol

            return ArithmeticCheckResult(
                result_alias=alias, rule="percent_of",
                status="passed" if ok else "failed",
                expected=round(expected, 4),
                actual=round(result_val, 4),
                delta=round(result_val - expected, 4),
                tolerance=tol,
                operand_aliases=[base_key],
                message=None if ok else (custom_msg or (
                    f"'{label}' should be {pct}% of '{base_key}' "
                    f"(expected {expected:.2f}), got {result_val:.2f}."
                )),
            )

        # Unknown rule
        return ArithmeticCheckResult(
            result_alias=alias, rule=constraint_rule, status="skipped",
            message=f"Unknown arithmetic rule '{constraint_rule}'.",
        )
