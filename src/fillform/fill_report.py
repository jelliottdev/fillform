"""Combined fill report — machine-readable and attorney-readable.

Packages the complete audit trail of a fill operation into a single object
that can be:

  - Serialised to JSON for storage and downstream processing
  - Rendered to Markdown for attorney review
  - Consumed as a structured ``review_queue`` for a UI

A ``FillReport`` is the answer to "what happened, what failed, and what do I
need to review before filing?"

Typical usage
-------------
::

    from fillform.fill_report import FillReport, ReviewItem

    report = FillReport.build(
        schema=schema,
        fill_result=fill_result,
        verification=verification_report,
        visual_qa=visual_qa_report,
        arithmetic=arithmetic_report,
        form_path="/path/to/source.pdf",
        output_path="/path/to/filled.pdf",
    )

    # For a review UI
    for item in report.review_queue():
        print(item.priority, item.alias, item.message)

    # For filing documentation
    print(report.to_markdown())

    # For downstream systems
    import json
    json.dumps(report.to_dict(), indent=2)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .arithmetic import ArithmeticReport
from .contracts import CanonicalSchema, FillPayload, VerificationReport
from .fill_engine import FillResult
from .visual_qa import VisualQAReport


# ---------------------------------------------------------------------------
# Review item — the unit of attorney attention
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewItem:
    """A single item requiring human review before the form is filed.

    Items are ranked by priority: 1 (must-fix) → 2 (should-review) → 3 (info).
    """

    priority: int            # 1=error 2=warning 3=info
    source: str              # "verification" | "visual_qa" | "arithmetic" | "fill_log"
    alias: str | None        # FXXX alias if field-specific
    field_name: str | None
    category: str            # "completeness" | "readback" | "format" | "constraint" | "visual" | "arithmetic"
    message: str
    suggested_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "priority": self.priority,
            "priority_label": {1: "error", 2: "warning", 3: "info"}.get(self.priority, "info"),
            "source": self.source,
            "alias": self.alias,
            "field_name": self.field_name,
            "category": self.category,
            "message": self.message,
            "suggested_action": self.suggested_action,
        }


# ---------------------------------------------------------------------------
# Fill report
# ---------------------------------------------------------------------------

@dataclass
class FillReport:
    """Aggregated fill operation report."""

    form_path: str | None
    output_path: str | None
    schema: CanonicalSchema
    payload: FillPayload | None
    fill_result: FillResult | None
    verification: VerificationReport | None
    visual_qa: VisualQAReport | None
    arithmetic: ArithmeticReport | None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        schema: CanonicalSchema,
        payload: FillPayload | None = None,
        fill_result: FillResult | None = None,
        verification: VerificationReport | None = None,
        visual_qa: VisualQAReport | None = None,
        arithmetic: ArithmeticReport | None = None,
        form_path: str | Path | None = None,
        output_path: str | Path | None = None,
    ) -> "FillReport":
        return cls(
            form_path=str(form_path) if form_path else None,
            output_path=str(output_path) if output_path else None,
            schema=schema,
            payload=payload,
            fill_result=fill_result,
            verification=verification,
            visual_qa=visual_qa,
            arithmetic=arithmetic,
        )

    # ------------------------------------------------------------------
    # High-level status
    # ------------------------------------------------------------------

    @property
    def is_ready_to_file(self) -> bool:
        """True when there are no error-priority review items."""
        return all(item.priority > 1 for item in self.review_queue())

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.review_queue() if i.priority == 1)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.review_queue() if i.priority == 2)

    @property
    def fill_success_rate(self) -> float | None:
        if self.fill_result is None:
            return None
        log = self.fill_result.fill_log
        if not log:
            return 1.0
        ok = sum(1 for v in log.values() if v.startswith("ok:"))
        return round(ok / len(log), 3)

    # ------------------------------------------------------------------
    # Review queue
    # ------------------------------------------------------------------

    def review_queue(self) -> list[ReviewItem]:
        """Return all issues ranked by priority (errors first)."""
        items: list[ReviewItem] = []

        # ── Fill log failures ──────────────────────────────────────────
        if self.fill_result:
            for key, status in self.fill_result.fill_log.items():
                if status.startswith("missing_field:"):
                    fn = status.split(":", 1)[1]
                    items.append(ReviewItem(
                        priority=2,
                        source="fill_log",
                        alias=key,
                        field_name=fn,
                        category="fill",
                        message=f"Field '{fn}' was not found in the PDF. Value was not written.",
                        suggested_action=(
                            "Verify the alias map is correct for this form version. "
                            "Re-run extract_form_fields if the form was recently updated."
                        ),
                    ))
                elif status.startswith("error:"):
                    parts = status.split(":", 2)
                    fn = parts[1] if len(parts) > 1 else key
                    err = parts[2] if len(parts) > 2 else "unknown error"
                    items.append(ReviewItem(
                        priority=1,
                        source="fill_log",
                        alias=key,
                        field_name=fn,
                        category="fill",
                        message=f"Write error on '{fn}': {err}",
                        suggested_action="Check field type and value format.",
                    ))

        # ── Verification issues ────────────────────────────────────────
        if self.verification:
            for check in self.verification.checks:
                for issue in check.issues:
                    sev = issue.severity.lower()
                    priority = 1 if sev == "error" else 2 if sev == "warning" else 3
                    cat = check.category or "verification"
                    items.append(ReviewItem(
                        priority=priority,
                        source="verification",
                        alias=issue.field,
                        field_name=issue.metadata.get("field_name"),
                        category=cat,
                        message=issue.message,
                        suggested_action=_suggested_action_for_rule(issue.rule),
                    ))

        # ── Arithmetic failures ────────────────────────────────────────
        if self.arithmetic:
            for check in self.arithmetic.failed:
                items.append(ReviewItem(
                    priority=1,
                    source="arithmetic",
                    alias=check.result_alias,
                    field_name=None,
                    category="arithmetic",
                    message=check.message or (
                        f"Arithmetic check '{check.rule}' failed on '{check.result_alias}'."
                    ),
                    suggested_action=(
                        "Recalculate this field. Totals that don't match line items "
                        "will likely trigger a trustee objection."
                    ),
                ))

        # ── Visual QA issues ───────────────────────────────────────────
        if self.visual_qa:
            for vr in self.visual_qa.field_issues:
                priority = 2 if "overflow" in vr.status else 2
                if "empty" in vr.status:
                    priority = 1
                items.append(ReviewItem(
                    priority=priority,
                    source="visual_qa",
                    alias=vr.alias,
                    field_name=vr.field_name,
                    category="visual",
                    message=vr.message or f"Visual issue on '{vr.alias}': {vr.status}",
                    suggested_action=_suggested_action_for_visual(vr.status),
                ))

        # Sort: errors first, then warnings, then info; within priority, by alias
        items.sort(key=lambda i: (i.priority, i.alias or "", i.source))
        return items

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        queue = self.review_queue()
        return {
            "created_at": self.created_at.isoformat(),
            "form_path": self.form_path,
            "output_path": self.output_path,
            "schema": {
                "form_family": self.schema.form_family,
                "version": self.schema.version,
                "field_count": len(self.schema.fields),
                "required_count": sum(1 for f in self.schema.fields if f.is_required),
            },
            "is_ready_to_file": self.is_ready_to_file,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "fill_success_rate": self.fill_success_rate,
            "fill_log": self.fill_result.fill_log if self.fill_result else None,
            "changed_fields": self.fill_result.changed_fields if self.fill_result else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "arithmetic": self.arithmetic.to_dict() if self.arithmetic else None,
            "visual_qa": self.visual_qa.to_dict() if self.visual_qa else None,
            "review_queue": [item.to_dict() for item in queue],
        }

    # ------------------------------------------------------------------
    # Human-readable Markdown report
    # ------------------------------------------------------------------

    def to_markdown(self) -> str:
        queue = self.review_queue()
        errors = [i for i in queue if i.priority == 1]
        warnings = [i for i in queue if i.priority == 2]
        info = [i for i in queue if i.priority == 3]

        lines: list[str] = [
            f"# Fill Report — {self.schema.form_family} v{self.schema.version}",
            f"Generated: {self.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
            "",
        ]

        # Filing readiness banner
        if self.is_ready_to_file:
            lines.append("> **READY TO FILE** — No blocking errors found.")
        else:
            lines.append(
                f"> **NOT READY TO FILE** — {self.error_count} error(s) must be resolved."
            )
        lines.append("")

        # Summary table
        lines += [
            "## Summary",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Form | {self.schema.form_family} v{self.schema.version} |",
            f"| Fields | {len(self.schema.fields)} total, "
            f"{sum(1 for f in self.schema.fields if f.is_required)} required |",
        ]

        if self.fill_result:
            ok = sum(1 for v in self.fill_result.fill_log.values() if v.startswith("ok:"))
            total = len(self.fill_result.fill_log)
            lines.append(f"| Fill success | {ok}/{total} fields ({ok/max(total,1):.0%}) |")
            lines.append(f"| Fields changed | {len(self.fill_result.changed_fields)} |")

        if self.verification:
            verified = "✓ Yes" if self.verification.verified else "✗ No"
            lines.append(f"| Verification | {verified} |")

        if self.arithmetic:
            arith_ok = "✓ Yes" if self.arithmetic.is_valid else f"✗ No ({len(self.arithmetic.failed)} failure(s))"
            lines.append(f"| Arithmetic | {arith_ok} |")

        if self.visual_qa:
            vis_ok = "✓ No issues" if not self.visual_qa.has_issues else f"✗ {self.visual_qa.issue_count} issue(s)"
            lines.append(f"| Visual QA | {vis_ok} |")

        lines += ["", f"| **Review items** | **{len(errors)} error(s), {len(warnings)} warning(s)** |", ""]

        # Errors
        if errors:
            lines += ["## ⛔ Errors — Must Fix Before Filing", ""]
            for item in errors:
                tag = f"`{item.alias}`" if item.alias else "—"
                lines.append(f"### {tag} · {item.category}")
                lines.append(f"**Source:** {item.source}")
                lines.append(f"**Issue:** {item.message}")
                if item.suggested_action:
                    lines.append(f"**Action:** {item.suggested_action}")
                lines.append("")

        # Warnings
        if warnings:
            lines += ["## ⚠ Warnings — Review Before Filing", ""]
            for item in warnings:
                tag = f"`{item.alias}`" if item.alias else "—"
                lines.append(f"### {tag} · {item.category}")
                lines.append(f"**Source:** {item.source}")
                lines.append(f"**Issue:** {item.message}")
                if item.suggested_action:
                    lines.append(f"**Action:** {item.suggested_action}")
                lines.append("")

        # Field-by-field table
        if self.fill_result and self.fill_result.fill_log:
            lines += ["## Field Fill Log", ""]
            lines.append("| Alias | Field Name | Status |")
            lines.append("|-------|------------|--------|")
            for key in sorted(self.fill_result.fill_log):
                status = self.fill_result.fill_log[key]
                icon = "✓" if status.startswith("ok:") else "✗"
                short = status.split(":", 1)[1] if ":" in status else status
                lines.append(f"| `{key}` | {short} | {icon} {status.split(':')[0]} |")
            lines.append("")

        # Changed fields
        if self.fill_result and self.fill_result.changed_fields:
            lines += ["## Changed Fields", ""]
            lines.append("| Field | Before | After |")
            lines.append("|-------|--------|-------|")
            for change in self.fill_result.changed_fields:
                fn = change.get("field_name", change.get("input_key", "?"))
                before = change.get("before", "")
                after = change.get("after", "")
                lines.append(f"| {fn} | `{before}` | `{after}` |")
            lines.append("")

        # Output path
        if self.output_path:
            lines += [
                "---",
                "",
                f"**Filled PDF:** `{self.output_path}`",
                "",
            ]

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Action helpers
# ---------------------------------------------------------------------------

def _suggested_action_for_rule(rule: str) -> str | None:
    return {
        "required_field_missing": "Provide a value for this field. It is required for the form to be accepted.",
        "value_mismatch": "Recheck the intended value and re-run the fill. May indicate a checkbox/radio logic error.",
        "invalid_date_format": "Use MM/DD/YYYY format for dates on this form.",
        "invalid_number_format": "Enter a plain number (e.g. 1234.56, not '$1,234.56') and re-fill.",
        "invalid_ssn_format": "Provide SSN in XXX-XX-XXXX format.",
        "invalid_zip_format": "Provide a 5-digit or 9-digit (ZIP+4) ZIP code.",
        "min_value": "Correct the value — it is below the allowed minimum.",
        "max_value": "Correct the value — it exceeds the allowed maximum.",
        "enum": "Choose one of the allowed values listed in the issue details.",
        "required_if": "This field is conditionally required based on another field's value. Provide a value.",
        "exclusive_with": "Only one field in this group may be selected. Deselect the extras.",
        "pattern": "Format the value according to the expected pattern.",
    }.get(rule)


def _suggested_action_for_visual(status: str) -> str | None:
    return {
        "possibly_empty": (
            "Open the filled PDF and visually confirm this field shows a value. "
            "If blank, re-fill with a shorter or differently formatted value."
        ),
        "possible_overflow": (
            "The value may be too long for the field box. "
            "Abbreviate if possible, or check whether the form accepts multi-line text."
        ),
        "checkbox_mismatch": (
            "The checkbox visual state may not match the intended selection. "
            "Open the PDF and confirm the checkbox appears correctly checked or unchecked."
        ),
    }.get(status)
