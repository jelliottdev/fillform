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

    # ------------------------------------------------------------------
    # Pixel-level rendered inspection (opt-in — slower than text extraction)
    # ------------------------------------------------------------------

    def render_check(
        self,
        filled_pdf: str | Path,
        schema: CanonicalSchema,
        payload: FillPayload,
        dpi: int = 150,
    ) -> VisualQAReport:
        """Render each page to pixels and inspect field regions at the pixel level.

        This is more accurate than :meth:`check` (which relies on text extraction)
        because it catches:

        - Fields with white/invisible text that ``get_text()`` still reports
        - Checkboxes whose appearance stream does not match the stored value
          (i.e. the mark is missing even though the field is "checked")
        - Text that actually overflows its box in the rendered output

        Parameters
        ----------
        filled_pdf:
            Path to the filled PDF output.
        schema:
            Canonical schema providing field geometry.
        payload:
            Fill payload (what was intended per field).
        dpi:
            Render resolution.  150 is a good balance; use 200 for dense forms.

        Returns
        -------
        :class:`VisualQAReport` — same structure as :meth:`check`, so both can
        be combined or compared.
        """
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError(
                "PyMuPDF (fitz) is required for pixel-level visual QA. "
                "Install it with: pip install pymupdf"
            ) from exc

        pdf = Path(filled_pdf)
        scale = dpi / 72.0
        mat = fitz.Matrix(scale, scale)

        alias_to_field: dict[str, CanonicalField] = {f.alias: f for f in schema.fields}
        field_name_to_field: dict[str, CanonicalField] = {f.field_name: f for f in schema.fields}

        intended: dict[str, Any] = {}
        for key, value in payload.values.items():
            canonical = alias_to_field.get(str(key)) or field_name_to_field.get(str(key))
            if canonical is not None:
                intended[canonical.alias] = value

        # Group fields by page so we only render each page once
        fields_by_page: dict[int, list[CanonicalField]] = {}
        for f in schema.fields:
            if f.alias in intended:
                fields_by_page.setdefault(f.page, []).append(f)

        issues: list[VisualFieldResult] = []
        checked = 0

        with fitz.open(str(pdf)) as doc:
            widgets_by_name: dict[str, list[Any]] = {}
            for page in doc:
                for w in page.widgets() or []:
                    if w.field_name:
                        widgets_by_name.setdefault(str(w.field_name), []).append(w)

            for page_idx, page_fields in sorted(fields_by_page.items()):
                if page_idx >= doc.page_count:
                    continue
                page = doc.load_page(page_idx)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                pix_width = pix.width
                samples: bytes = pix.samples  # RGB, 3 bytes per pixel

                for canonical in page_fields:
                    expected_value = intended.get(canonical.alias)
                    expected_text = "" if expected_value is None else str(expected_value)
                    checked += 1

                    ft = (canonical.field_type or "").lower()
                    is_checkbox = ft in {"btn", "button"} or any(
                        "checkbox" in str(getattr(w, "field_type_string", "")).lower()
                        or str(getattr(w, "field_type", "")) == "2"
                        for w in (widgets_by_name.get(canonical.field_name) or [])
                    )

                    x0, y0, x1, y1 = canonical.bbox
                    px0 = max(0, int(x0 * scale))
                    py0 = max(0, int(y0 * scale))
                    px1 = min(pix_width, int(x1 * scale))
                    py1 = min(pix.height, int(y1 * scale))

                    if px1 <= px0 or py1 <= py0:
                        continue

                    if is_checkbox:
                        result = self._pixel_check_checkbox(
                            alias=canonical.alias,
                            field_name=canonical.field_name,
                            page_idx=page_idx,
                            samples=samples,
                            pix_width=pix_width,
                            px0=px0, py0=py0, px1=px1, py1=py1,
                            expected_text=expected_text,
                        )
                    else:
                        result = self._pixel_check_text_field(
                            alias=canonical.alias,
                            field_name=canonical.field_name,
                            page_idx=page_idx,
                            samples=samples,
                            pix_width=pix_width,
                            px0=px0, py0=py0, px1=px1, py1=py1,
                            expected_text=expected_text,
                            field_width_pts=x1 - x0,
                        )

                    if result.status != "ok":
                        issues.append(result)

        return VisualQAReport(
            pdf_path=str(pdf),
            fields_checked=checked,
            field_issues=issues,
            generated_at=datetime.now(timezone.utc),
        )

    def _pixel_white_ratio(
        self,
        samples: bytes,
        pix_width: int,
        px0: int, py0: int, px1: int, py1: int,
        threshold: int = 240,
        step: int = 3,
    ) -> float:
        """Return fraction of sampled pixels in the region that are near-white."""
        white = 0
        total = 0
        n_samples = len(samples)
        for py in range(py0, py1, step):
            row = py * pix_width * 3
            for px in range(px0, px1, step):
                idx = row + px * 3
                if idx + 2 >= n_samples:
                    continue
                r, g, b = samples[idx], samples[idx + 1], samples[idx + 2]
                total += 1
                if r >= threshold and g >= threshold and b >= threshold:
                    white += 1
        return white / max(total, 1)

    def _pixel_check_text_field(
        self,
        alias: str,
        field_name: str,
        page_idx: int,
        samples: bytes,
        pix_width: int,
        px0: int, py0: int, px1: int, py1: int,
        expected_text: str,
        field_width_pts: float,
    ) -> VisualFieldResult:
        white_ratio = self._pixel_white_ratio(samples, pix_width, px0, py0, px1, py1)

        if expected_text and white_ratio > 0.97:
            return VisualFieldResult(
                alias=alias, field_name=field_name, page=page_idx,
                status="possibly_empty",
                message=(
                    f"Field has stored value '{expected_text[:40]}' but the rendered "
                    f"region is {white_ratio:.0%} white — value may be invisible "
                    "(white text, rendering failure, or appearance-stream issue)."
                ),
                metadata={"white_ratio": round(white_ratio, 3), "check_method": "pixel"},
            )

        # Check right-edge pixel strip for content spilling past the field boundary
        if expected_text and (px1 - px0) > 10:
            edge_w = max(2, (px1 - px0) // 20)
            edge_ratio = self._pixel_white_ratio(
                samples, pix_width, px1 - edge_w, py0, px1, py1, threshold=200
            )
            if edge_ratio < 0.75 and len(expected_text) * 5.5 > field_width_pts:
                return VisualFieldResult(
                    alias=alias, field_name=field_name, page=page_idx,
                    status="possible_overflow",
                    message=(
                        f"Dark pixels at right edge of '{field_name}' "
                        f"({edge_ratio:.0%} non-white) — text may be clipped."
                    ),
                    metadata={"edge_white_ratio": round(edge_ratio, 3), "check_method": "pixel"},
                )

        return VisualFieldResult(alias=alias, field_name=field_name, page=page_idx, status="ok")

    def _pixel_check_checkbox(
        self,
        alias: str,
        field_name: str,
        page_idx: int,
        samples: bytes,
        pix_width: int,
        px0: int, py0: int, px1: int, py1: int,
        expected_text: str,
    ) -> VisualFieldResult:
        expected_selected = expected_text.lower() not in {"off", "", "false", "0", "no"}
        white_ratio = self._pixel_white_ratio(samples, pix_width, px0, py0, px1, py1)
        # A checked checkbox should have non-white pixels (the checkmark)
        visually_appears_checked = white_ratio < 0.80

        if expected_selected and not visually_appears_checked:
            return VisualFieldResult(
                alias=alias, field_name=field_name, page=page_idx,
                status="checkbox_mismatch",
                message=(
                    f"Checkbox should be checked but rendered region is "
                    f"{white_ratio:.0%} white — check mark may be missing."
                ),
                metadata={"white_ratio": round(white_ratio, 3), "check_method": "pixel"},
            )
        if not expected_selected and visually_appears_checked:
            return VisualFieldResult(
                alias=alias, field_name=field_name, page=page_idx,
                status="checkbox_mismatch",
                message=(
                    f"Checkbox should be unchecked but rendered region is only "
                    f"{white_ratio:.0%} white — may appear checked."
                ),
                metadata={"white_ratio": round(white_ratio, 3), "check_method": "pixel"},
            )

        return VisualFieldResult(alias=alias, field_name=field_name, page=page_idx, status="ok")
