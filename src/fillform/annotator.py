"""PDF annotator: overlays vibrant FXXX alias labels on AcroForm field positions.

The annotated PDF is the input for vision-model analysis.  Each field is covered
by a filled, high-contrast rectangle with the alias drawn in white — making every
field immediately legible to both humans and vision models.
"""

from __future__ import annotations

from pathlib import Path

from .field_alias import AliasMap

# Vibrant orange fill (R, G, B in 0-1 range) — legible against most backgrounds.
_FILL_COLOR = (1.0, 0.45, 0.0)
_TEXT_COLOR = (1.0, 1.0, 1.0)  # white
_BORDER_COLOR = (0.7, 0.25, 0.0)  # darker orange border for contrast
_DEFAULT_FONT_SIZE = 9
_MIN_FONT_SIZE = 6
_PADDING = 1.0  # pts of inset padding inside the rect


class PdfAnnotator:
    """Renders an annotated copy of a PDF with FXXX labels at each field's position.

    Requires ``PyMuPDF`` (``fitz``).  Field coordinates are sourced directly from
    the live PDF widgets so they are always in fitz device-space (top-left origin),
    eliminating any coordinate-system ambiguity with stored bboxes.
    """

    def annotate(
        self,
        source_pdf: str | Path,
        alias_map: AliasMap,
        output_path: str | Path,
    ) -> Path:
        """Write an annotated copy of *source_pdf* to *output_path*.

        The method overlays each field listed in *alias_map* with a bright orange
        rectangle and its FXXX alias label.  Fields not present in *alias_map* are
        left untouched.

        Returns the resolved *output_path*.
        """
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError(
                "PyMuPDF (fitz) is required for PDF annotation. "
                "Install it with: pip install pymupdf"
            ) from exc

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        # Build a lookup from field name -> alias for quick matching
        name_to_alias = {w.name: alias for alias, w in alias_map.field_widgets.items()}

        with fitz.open(str(source_pdf)) as doc:
            for page_index in range(doc.page_count):
                page = doc.load_page(page_index)

                # Gather all widgets on this page keyed by field name
                live_widgets = {
                    str(w.field_name or ""): w.rect
                    for w in (page.widgets() or [])
                }

                for alias, stored_widget in alias_map.field_widgets.items():
                    if stored_widget.page != page_index:
                        continue

                    # Prefer live widget rect (fitz top-left coords); fall back to
                    # converting stored PDF-space bbox.
                    if stored_widget.name in live_widgets:
                        rect = live_widgets[stored_widget.name]
                    else:
                        rect = _pdf_bbox_to_fitz_rect(
                            stored_widget.bbox, page.rect.height
                        )

                    if rect is None or rect.is_empty or rect.is_infinite:
                        continue

                    # Shrink slightly so we don't bleed over form borders
                    inner = rect + (_PADDING, _PADDING, -_PADDING, -_PADDING)
                    if inner.is_empty:
                        inner = rect

                    # Filled orange rectangle
                    page.draw_rect(
                        inner,
                        color=_BORDER_COLOR,
                        fill=_FILL_COLOR,
                        width=0.5,
                    )

                    # Alias label
                    font_size = _compute_font_size(inner)
                    page.insert_textbox(
                        inner,
                        alias,
                        fontsize=font_size,
                        color=_TEXT_COLOR,
                        align=1,  # centre
                    )

            doc.save(str(output))

        return output


def _pdf_bbox_to_fitz_rect(bbox: tuple[float, float, float, float], page_height: float):
    """Convert a PDF bottom-left-origin bbox to a fitz top-left-origin Rect."""
    try:
        import fitz
    except ImportError:
        return None

    x0, y0, x1, y1 = bbox
    return fitz.Rect(x0, page_height - y1, x1, page_height - y0)


def _compute_font_size(rect) -> float:
    """Pick the largest font size that fits the label inside *rect*."""
    height = rect.height
    width = rect.width
    # Rough heuristic: 4-character label (e.g. "F001") at ~6px/char width
    size_by_height = height * 0.65
    size_by_width = width / 4.5
    size = min(size_by_height, size_by_width, _DEFAULT_FONT_SIZE)
    return max(size, _MIN_FONT_SIZE)
