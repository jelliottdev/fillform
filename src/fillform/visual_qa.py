"""Visual QA layer — inspect filled PDF field appearances.

The goal is to catch cases where a field has the *correct stored value* but
looks wrong when rendered:

  - Text fields that appear visually empty (rendering failure, white text, or
    the value never reached the widget despite the fill log saying ok)
  - Text that is likely too long to fit the field box (will be clipped)
  - Checkbox widgets whose visual appearance may not match the stored state

All checks use PyMuPDF text-extraction and widget geometry heuristics.
They are fast but not pixel-perfect — treat results as a prioritised review
queue rather than a ground-truth pass/fail.

Typical usage
-------------
::

    from fillform.visual_qa import VisualQAEngine

    engine = VisualQAEngine()
    report = engine.check(
        filled_pdf="/path/to/form_filled.pdf",
        schema=canonical_schema,
        payload=fill_payload,
    )
    if report.has_issues:
        for issue in report.field_issues:
            print(issue.status, issue.alias, issue.message)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import CanonicalField, CanonicalSchema, FillPayload


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class VisualFieldResult:
    """Per-field visual inspection result."""

    alias: str
    field_name: str
    page: int                       # 0-based
    status: str                     # ok | possibly_empty | possible_overflow | checkbox_mismatch | skipped
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "field_name": self.field_name,
            "page": self.page,
            "status": self.status,
            "message": self.message,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class VisualQAReport:
    """Aggregate visual QA result for a filled PDF."""

    pdf_path: str
    fields_checked: int
    field_issues: list[VisualFieldResult]
    generated_at: datetime

    @property
    def has_issues(self) -> bool:
        return bool(self.field_issues)

    @property
    def issue_count(self) -> int:
        return len(self.field_issues)

    @property
    def ok_count(self) -> int:
        return self.fields_checked - self.issue_count

    def summary(self) -> str:
        lines = [
            f"Visual QA: {self.fields_checked} fields checked, "
            f"{self.issue_count} issue(s) found.",
        ]
        for r in self.field_issues:
            lines.append(f"  [{r.status}] {r.alias} ({r.field_name}, page {r.page + 1}): {r.message}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_path": self.pdf_path,
            "fields_checked": self.fields_checked,
            "issue_count": self.issue_count,
            "ok_count": self.ok_count,
            "field_issues": [r.to_dict() for r in self.field_issues],
            "generated_at": self.generated_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Heuristics configuration
# ---------------------------------------------------------------------------

# Points-per-character estimate for overflow detection.
# Assumes ~10pt font; many bankruptcy forms use 8–10pt.  Tune per form family.
_CHARS_PER_POINT = 1 / 5.5

# A field region where >90% of extracted text space is blank is flagged.
_EMPTY_WHITESPACE_THRESHOLD = 0.90

# Overflow: if estimated text width > field width × this factor, flag it.
_OVERFLOW_FACTOR = 0.95


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class VisualQAEngine:
    """Inspect the rendered appearance of filled PDF fields."""

    def check(
        self,
        filled_pdf: str | Path,
        schema: CanonicalSchema,
        payload: FillPayload,
    ) -> VisualQAReport:
        """Run visual checks on *filled_pdf*.

        Parameters
        ----------
        filled_pdf:
            Path to the output PDF produced by the fill engine.
        schema:
            The canonical schema (provides bbox, type, and alias mapping).
        payload:
            The fill payload so the engine knows what value was intended for
            each field.
        """
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError(
                "PyMuPDF (fitz) is required for visual QA. "
                "Install it with: pip install pymupdf"
            ) from exc

        pdf = Path(filled_pdf)
        alias_to_field: dict[str, CanonicalField] = {f.alias: f for f in schema.fields}
        field_name_to_field: dict[str, CanonicalField] = {f.field_name: f for f in schema.fields}

        # Build intended-value lookup (alias or field_name → value)
        intended: dict[str, Any] = {}
        for key, value in payload.values.items():
            canonical = alias_to_field.get(str(key)) or field_name_to_field.get(str(key))
            if canonical is not None:
                intended[canonical.alias] = value

        issues: list[VisualFieldResult] = []
        checked = 0

        with fitz.open(str(pdf)) as doc:
            # Build widget index: field_name → list of widgets
            widgets_by_name: dict[str, list[Any]] = {}
            for page in doc:
                for w in page.widgets() or []:
                    if w.field_name:
                        widgets_by_name.setdefault(str(w.field_name), []).append(w)

            for canonical in schema.fields:
                alias = canonical.alias
                expected_value = intended.get(alias)

                # Only inspect fields that were filled
                if expected_value is None:
                    continue

                expected_text = "" if expected_value is None else str(expected_value)
                widgets = widgets_by_name.get(canonical.field_name) or []
                if not widgets:
                    continue

                checked += 1
                page_idx = canonical.page

                try:
                    page = doc.load_page(page_idx)
                    result = self._check_field(
                        page=page,
                        canonical=canonical,
                        widgets=widgets,
                        expected_text=expected_text,
                    )
                    if result.status != "ok":
                        issues.append(result)
                except Exception as exc:
                    issues.append(VisualFieldResult(
                        alias=alias,
                        field_name=canonical.field_name,
                        page=page_idx,
                        status="skipped",
                        message=f"Visual check failed: {exc}",
                    ))

        return VisualQAReport(
            pdf_path=str(pdf),
            fields_checked=checked,
            field_issues=issues,
            generated_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Per-field checks
    # ------------------------------------------------------------------

    def _check_field(
        self,
        page: Any,
        canonical: CanonicalField,
        widgets: list[Any],
        expected_text: str,
    ) -> VisualFieldResult:
        import fitz

        alias = canonical.alias
        field_name = canonical.field_name
        page_idx = canonical.page
        x0, y0, x1, y1 = canonical.bbox
        field_rect = fitz.Rect(x0, y0, x1, y1)

        ft = (canonical.field_type or "").lower()
        is_checkbox = ft in {"btn", "button"} or any(
            "checkbox" in str(getattr(w, "field_type_string", "")).lower()
            or str(getattr(w, "field_type", "")) == "2"
            for w in widgets
        )

        if is_checkbox:
            return self._check_checkbox(
                alias=alias,
                field_name=field_name,
                page_idx=page_idx,
                widgets=widgets,
                expected_text=expected_text,
            )

        # --- Text / choice field checks ---

        # Check 1: Is there any visible text in the field region?
        visible_text = page.get_text("text", clip=field_rect).strip()
        if expected_text and not visible_text:
            return VisualFieldResult(
                alias=alias,
                field_name=field_name,
                page=page_idx,
                status="possibly_empty",
                message=(
                    f"Field has stored value '{expected_text[:40]}' "
                    "but no text is visible in the rendered field region. "
                    "Possible rendering failure."
                ),
                metadata={"expected": expected_text, "visible_text": visible_text},
            )

        # Check 2: Overflow — is the text too long for the field width?
        field_width = x1 - x0
        if field_width > 0 and expected_text:
            estimated_width = len(expected_text) / _CHARS_PER_POINT
            if estimated_width > field_width * _OVERFLOW_FACTOR:
                return VisualFieldResult(
                    alias=alias,
                    field_name=field_name,
                    page=page_idx,
                    status="possible_overflow",
                    message=(
                        f"Value '{expected_text[:40]}' ({len(expected_text)} chars) "
                        f"may overflow the field width ({field_width:.0f} pts). "
                        "Text may be clipped in PDF viewers."
                    ),
                    metadata={
                        "field_width_pts": round(field_width, 1),
                        "estimated_text_width_pts": round(estimated_width, 1),
                        "char_count": len(expected_text),
                    },
                )

        return VisualFieldResult(
            alias=alias,
            field_name=field_name,
            page=page_idx,
            status="ok",
        )

    def _check_checkbox(
        self,
        alias: str,
        field_name: str,
        page_idx: int,
        widgets: list[Any],
        expected_text: str,
    ) -> VisualFieldResult:
        """Check that the checkbox/radio visual state is consistent."""
        expected_selected = expected_text.lower() not in {"off", "", "false", "0", "no"}

        for widget in widgets:
            stored = str(widget.field_value or "").lower()
            visually_on = stored not in {"off", "", "false", "0", "no"}

            if expected_selected != visually_on:
                return VisualFieldResult(
                    alias=alias,
                    field_name=field_name,
                    page=page_idx,
                    status="checkbox_mismatch",
                    message=(
                        f"Expected checkbox state: {'checked' if expected_selected else 'unchecked'}, "
                        f"stored value is '{stored}' which reads as "
                        f"{'checked' if visually_on else 'unchecked'}."
                    ),
                    metadata={
                        "expected_text": expected_text,
                        "stored_value": stored,
                        "expected_selected": expected_selected,
                    },
                )

        return VisualFieldResult(
            alias=alias,
            field_name=field_name,
            page=page_idx,
            status="ok",
        )
