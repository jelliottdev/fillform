"""Fill quality metrics — explicit quality bars for a legal-grade engine.

This module defines and measures the quality targets from the maturity checklist:

  - Fill accuracy rate       % of fields filled correctly (no write errors)
  - Required field coverage  % of required fields present in the payload
  - Verification match rate  % of intended values that read back correctly
  - Visual pass rate         % of filled fields with no visual issues
  - Arithmetic accuracy      % of arithmetic constraints that pass
  - Overall quality score    Weighted composite of the above

Each metric has a target threshold.  The module reports whether those targets
are met and by how much.  This is the measurement layer — it tells you whether
FillForm is performing at legal-grade reliability on a given form and payload.

Example
-------
::

    from fillform.quality import QualityMetrics, QualityReport

    report = QualityReport.from_artifacts(
        schema=schema,
        fill_result=fill_result,
        verification=verification_report,
        visual_qa=visual_qa_report,
        arithmetic=arithmetic_report,
    )
    print(report.summary())
    print("Overall score:", report.overall_score)
    print("Legal-grade?", report.meets_legal_grade_threshold)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .arithmetic import ArithmeticReport
from .contracts import CanonicalSchema, FillPayload, VerificationReport
from .fill_engine import FillResult
from .visual_qa import VisualQAReport


# ---------------------------------------------------------------------------
# Thresholds for "legal-grade engine" (Stage 3)
# ---------------------------------------------------------------------------

FILL_ACCURACY_TARGET = 0.97          # 97% of fields written without error
REQUIRED_COVERAGE_TARGET = 1.00      # 100% of required fields present
VERIFICATION_MATCH_TARGET = 0.97     # 97% of readback values match intended
VISUAL_PASS_TARGET = 0.95            # 95% of fields have no visual issues
ARITHMETIC_PASS_TARGET = 1.00        # 100% of arithmetic constraints pass
OVERALL_SCORE_TARGET = 0.95          # Weighted composite


# Weights for overall score
_WEIGHTS = {
    "fill_accuracy": 0.30,
    "required_coverage": 0.25,
    "verification_match": 0.25,
    "visual_pass": 0.10,
    "arithmetic_pass": 0.10,
}


# ---------------------------------------------------------------------------
# Individual metric
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityMetric:
    """Single quality dimension with actual value, target, and pass/fail status."""

    name: str
    actual: float                 # 0.0 – 1.0
    target: float                 # 0.0 – 1.0
    weight: float                 # contribution to overall score
    numerator: int                # fields/checks that passed
    denominator: int              # total fields/checks evaluated
    description: str

    @property
    def passes(self) -> bool:
        return self.actual >= self.target

    @property
    def gap(self) -> float:
        """How far below target (negative means above target)."""
        return self.target - self.actual

    @property
    def weighted_score(self) -> float:
        return self.actual * self.weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "actual": round(self.actual, 4),
            "target": round(self.target, 4),
            "passes": self.passes,
            "gap": round(self.gap, 4),
            "weight": self.weight,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "description": self.description,
        }

    def __str__(self) -> str:
        icon = "✓" if self.passes else "✗"
        return (
            f"{icon} {self.name}: {self.actual:.1%} "
            f"(target {self.target:.0%}, {self.numerator}/{self.denominator})"
        )


# ---------------------------------------------------------------------------
# Quality report
# ---------------------------------------------------------------------------

@dataclass
class QualityReport:
    """Aggregate quality measurement across all fill dimensions."""

    metrics: list[QualityMetric] = field(default_factory=list)
    schema_family: str = "unknown"
    schema_version: str = "1"
    form_path: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_artifacts(
        cls,
        schema: CanonicalSchema,
        fill_result: FillResult | None = None,
        verification: VerificationReport | None = None,
        visual_qa: VisualQAReport | None = None,
        arithmetic: ArithmeticReport | None = None,
        payload: FillPayload | None = None,
        form_path: str | None = None,
    ) -> "QualityReport":
        metrics: list[QualityMetric] = []

        # ── Fill accuracy ──────────────────────────────────────────────
        if fill_result is not None:
            log = fill_result.fill_log
            total = len(log)
            ok = sum(1 for v in log.values() if v.startswith("ok:"))
            metrics.append(QualityMetric(
                name="fill_accuracy",
                actual=ok / max(total, 1),
                target=FILL_ACCURACY_TARGET,
                weight=_WEIGHTS["fill_accuracy"],
                numerator=ok,
                denominator=total,
                description="Fraction of fill attempts that succeeded without error or missing-field.",
            ))

        # ── Required field coverage ────────────────────────────────────
        required_fields = [f for f in schema.fields if f.is_required]
        total_req = len(required_fields)
        if total_req > 0 and payload is not None:
            provided_keys = set(payload.values.keys())
            covered = sum(
                1 for f in required_fields
                if (f.alias in provided_keys or f.field_name in provided_keys)
                and payload.values.get(f.alias) is not None
                and str(payload.values.get(f.alias, "")).strip() != ""
            )
            metrics.append(QualityMetric(
                name="required_coverage",
                actual=covered / total_req,
                target=REQUIRED_COVERAGE_TARGET,
                weight=_WEIGHTS["required_coverage"],
                numerator=covered,
                denominator=total_req,
                description="Fraction of required schema fields with a non-empty value in the payload.",
            ))
        elif total_req == 0:
            metrics.append(QualityMetric(
                name="required_coverage",
                actual=1.0,
                target=REQUIRED_COVERAGE_TARGET,
                weight=_WEIGHTS["required_coverage"],
                numerator=0,
                denominator=0,
                description="No required fields defined in schema — coverage is 100% by default.",
            ))

        # ── Verification match rate ────────────────────────────────────
        if verification is not None:
            readback_checks = [c for c in verification.checks if c.check_id == "pdf_readback"]
            if readback_checks:
                rc = readback_checks[0]
                checked = rc.metadata.get("checked", 0)
                mismatches = len(rc.issues)
                matched = max(0, checked - mismatches)
                metrics.append(QualityMetric(
                    name="verification_match",
                    actual=matched / max(checked, 1),
                    target=VERIFICATION_MATCH_TARGET,
                    weight=_WEIGHTS["verification_match"],
                    numerator=matched,
                    denominator=checked,
                    description="Fraction of filled fields whose stored value matches the intended value on readback.",
                ))

        # ── Visual pass rate ───────────────────────────────────────────
        if visual_qa is not None and visual_qa.fields_checked > 0:
            total_vis = visual_qa.fields_checked
            issues = visual_qa.issue_count
            passed = total_vis - issues
            metrics.append(QualityMetric(
                name="visual_pass",
                actual=passed / total_vis,
                target=VISUAL_PASS_TARGET,
                weight=_WEIGHTS["visual_pass"],
                numerator=passed,
                denominator=total_vis,
                description="Fraction of filled fields with no visual appearance issues (overflow, empty, mismatch).",
            ))

        # ── Arithmetic accuracy ────────────────────────────────────────
        if arithmetic is not None and arithmetic.checks:
            non_skipped = [c for c in arithmetic.checks if c.status != "skipped"]
            total_arith = len(non_skipped)
            passed_arith = sum(1 for c in non_skipped if c.status == "passed")
            if total_arith > 0:
                metrics.append(QualityMetric(
                    name="arithmetic_pass",
                    actual=passed_arith / total_arith,
                    target=ARITHMETIC_PASS_TARGET,
                    weight=_WEIGHTS["arithmetic_pass"],
                    numerator=passed_arith,
                    denominator=total_arith,
                    description="Fraction of cross-field arithmetic constraints (sums, totals) that pass.",
                ))

        return cls(
            metrics=metrics,
            schema_family=schema.form_family,
            schema_version=schema.version,
            form_path=form_path,
        )

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def overall_score(self) -> float:
        """Weighted composite of all measured dimensions (0.0 – 1.0)."""
        if not self.metrics:
            return 0.0
        total_weight = sum(m.weight for m in self.metrics)
        if total_weight == 0:
            return 0.0
        weighted_sum = sum(m.actual * m.weight for m in self.metrics)
        return round(weighted_sum / total_weight, 4)

    @property
    def meets_legal_grade_threshold(self) -> bool:
        """True when all measured metrics meet their individual targets."""
        return all(m.passes for m in self.metrics)

    @property
    def failing_metrics(self) -> list[QualityMetric]:
        return [m for m in self.metrics if not m.passes]

    def get(self, name: str) -> QualityMetric | None:
        return next((m for m in self.metrics if m.name == name), None)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def summary(self) -> str:
        lines = [
            f"Quality Report — {self.schema_family} v{self.schema_version}",
            f"Overall score : {self.overall_score:.1%}  "
            f"(legal-grade threshold: {OVERALL_SCORE_TARGET:.0%})",
            f"Legal-grade   : {'YES ✓' if self.meets_legal_grade_threshold else 'NO ✗'}",
            "",
        ]
        for m in self.metrics:
            lines.append(f"  {m}")
        if self.failing_metrics:
            lines += [
                "",
                "Failing metrics:",
            ]
            for m in self.failing_metrics:
                lines.append(
                    f"  {m.name}: {m.actual:.1%} vs target {m.target:.0%} "
                    f"(gap {m.gap:.1%}, {m.denominator - m.numerator} failures)"
                )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at.isoformat(),
            "schema_family": self.schema_family,
            "schema_version": self.schema_version,
            "form_path": self.form_path,
            "overall_score": self.overall_score,
            "meets_legal_grade_threshold": self.meets_legal_grade_threshold,
            "targets": {
                "fill_accuracy": FILL_ACCURACY_TARGET,
                "required_coverage": REQUIRED_COVERAGE_TARGET,
                "verification_match": VERIFICATION_MATCH_TARGET,
                "visual_pass": VISUAL_PASS_TARGET,
                "arithmetic_pass": ARITHMETIC_PASS_TARGET,
                "overall": OVERALL_SCORE_TARGET,
            },
            "metrics": [m.to_dict() for m in self.metrics],
            "failing_metrics": [m.name for m in self.failing_metrics],
        }
