"""PDF structural extraction service with swappable provider adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


BBox = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class PageDimensions:
    page: int
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class FieldWidget:
    name: str
    field_type: str
    page: int
    bbox: BBox


@dataclass(frozen=True, slots=True)
class TextBlock:
    page: int
    text: str
    bbox: BBox


@dataclass(frozen=True, slots=True)
class LinePrimitive:
    page: int
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass(frozen=True, slots=True)
class RectanglePrimitive:
    page: int
    bbox: BBox


@dataclass(frozen=True, slots=True)
class StructuralRepresentation:
    page_dimensions: list[PageDimensions] = field(default_factory=list)
    field_widgets: list[FieldWidget] = field(default_factory=list)
    text_blocks: list[TextBlock] = field(default_factory=list)
    line_primitives: list[LinePrimitive] = field(default_factory=list)
    rectangle_primitives: list[RectanglePrimitive] = field(default_factory=list)


class PdfStructureAdapter(Protocol):
    """Provider-specific parser contract used by ``PdfStructureService``."""

    provider_name: str

    def extract(self, document_path: str | Path) -> StructuralRepresentation:
        """Extract structure from ``document_path``."""


class PypdfStructureAdapter:
    """Extract structural information from PDFs using ``pypdf``."""

    provider_name = "pypdf"

    def extract(self, document_path: str | Path) -> StructuralRepresentation:
        from pypdf import PdfReader
        from pypdf._page import ContentStream

        reader = PdfReader(str(document_path))

        page_dimensions: list[PageDimensions] = []
        field_widgets: list[FieldWidget] = []
        text_blocks: list[TextBlock] = []
        line_primitives: list[LinePrimitive] = []
        rectangle_primitives: list[RectanglePrimitive] = []

        for page_index, page in enumerate(reader.pages):
            media_box = page.mediabox
            page_dimensions.append(
                PageDimensions(
                    page=page_index,
                    width=float(media_box.width),
                    height=float(media_box.height),
                )
            )

            annotations = page.get("/Annots", []) or []
            for annotation_ref in annotations:
                annotation = annotation_ref.get_object()
                if annotation.get("/Subtype") != "/Widget":
                    continue
                rect = annotation.get("/Rect")
                if rect is None:
                    continue
                name = (
                    annotation.get("/T")
                    or annotation.get("/TU")
                    or annotation.get("/TM")
                    or f"widget_{page_index}_{len(field_widgets)}"
                )
                field_type = str(annotation.get("/FT") or "unknown").lstrip("/")
                field_widgets.append(
                    FieldWidget(
                        name=str(name),
                        field_type=field_type,
                        page=page_index,
                        bbox=(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])),
                    )
                )

            def _visitor_text(text: str, _cm: list[float], tm: list[float], *_args: object) -> None:
                cleaned = text.strip()
                if not cleaned:
                    return
                x, y = float(tm[4]), float(tm[5])
                # pypdf does not expose glyph-level boxes directly; estimate box from text length.
                char_width = 6.0
                height = 10.0
                text_blocks.append(
                    TextBlock(
                        page=page_index,
                        text=cleaned,
                        bbox=(x, y, x + len(cleaned) * char_width, y + height),
                    )
                )

            page.extract_text(visitor_text=_visitor_text)

            contents = page.get_contents()
            if contents:
                stream = ContentStream(contents, reader)
                last_move_to: tuple[float, float] | None = None
                for operands, operator in stream.operations:
                    if operator == b"re" and len(operands) == 4:
                        x, y, w, h = (float(value) for value in operands)
                        rectangle_primitives.append(
                            RectanglePrimitive(page=page_index, bbox=(x, y, x + w, y + h))
                        )
                    elif operator == b"m" and len(operands) == 2:
                        last_move_to = (float(operands[0]), float(operands[1]))
                    elif operator == b"l" and len(operands) == 2 and last_move_to is not None:
                        end = (float(operands[0]), float(operands[1]))
                        line_primitives.append(
                            LinePrimitive(page=page_index, start=last_move_to, end=end)
                        )

        return StructuralRepresentation(
            page_dimensions=page_dimensions,
            field_widgets=field_widgets,
            text_blocks=text_blocks,
            line_primitives=line_primitives,
            rectangle_primitives=rectangle_primitives,
        )


class PyMuPdfStructureAdapter:
    """Extract structural information from PDFs using ``PyMuPDF`` (fitz)."""

    provider_name = "pymupdf"

    def extract(self, document_path: str | Path) -> StructuralRepresentation:
        import fitz

        page_dimensions: list[PageDimensions] = []
        field_widgets: list[FieldWidget] = []
        text_blocks: list[TextBlock] = []
        line_primitives: list[LinePrimitive] = []
        rectangle_primitives: list[RectanglePrimitive] = []

        with fitz.open(str(document_path)) as doc:
            for page_index in range(doc.page_count):
                page = doc.load_page(page_index)

                page_dimensions.append(
                    PageDimensions(
                        page=page_index,
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                    )
                )

                for widget in page.widgets() or []:
                    rect = widget.rect
                    field_widgets.append(
                        FieldWidget(
                            name=str(widget.field_name or f"widget_{page_index}_{len(field_widgets)}"),
                            field_type=str(widget.field_type_string or "unknown"),
                            page=page_index,
                            bbox=(float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)),
                        )
                    )

                for block in page.get_text("blocks"):
                    x0, y0, x1, y1, text, *_ = block
                    cleaned = (text or "").strip()
                    if not cleaned:
                        continue
                    text_blocks.append(
                        TextBlock(
                            page=page_index,
                            text=cleaned,
                            bbox=(float(x0), float(y0), float(x1), float(y1)),
                        )
                    )

                for drawing in page.get_drawings():
                    for item in drawing.get("items", []):
                        op = item[0]
                        if op == "l":
                            p1, p2 = item[1], item[2]
                            line_primitives.append(
                                LinePrimitive(
                                    page=page_index,
                                    start=(float(p1.x), float(p1.y)),
                                    end=(float(p2.x), float(p2.y)),
                                )
                            )
                        elif op == "re":
                            rect = item[1]
                            rectangle_primitives.append(
                                RectanglePrimitive(
                                    page=page_index,
                                    bbox=(
                                        float(rect.x0),
                                        float(rect.y0),
                                        float(rect.x1),
                                        float(rect.y1),
                                    ),
                                )
                            )

        return StructuralRepresentation(
            page_dimensions=page_dimensions,
            field_widgets=field_widgets,
            text_blocks=text_blocks,
            line_primitives=line_primitives,
            rectangle_primitives=rectangle_primitives,
        )


class PdfStructureService:
    """Facade for extracting a provider-neutral ``StructuralRepresentation``."""

    def __init__(self, adapter: PdfStructureAdapter | None = None, provider: str | None = None) -> None:
        self._adapter = adapter or self._resolve_provider(provider)

    def extract(self, document_path: str | Path) -> StructuralRepresentation:
        return self._adapter.extract(document_path)

    def _resolve_provider(self, provider: str | None) -> PdfStructureAdapter:
        if provider is None or provider == "pypdf":
            try:
                import pypdf  # noqa: F401

                return PypdfStructureAdapter()
            except ImportError:
                if provider == "pypdf":
                    raise

        if provider is None or provider == "pymupdf":
            try:
                import fitz  # noqa: F401

                return PyMuPdfStructureAdapter()
            except ImportError:
                if provider == "pymupdf":
                    raise

        if provider is None:
            raise RuntimeError("No supported PDF structure provider found (tried: pypdf, pymupdf).")
        raise ValueError(f"Unsupported provider '{provider}'. Expected one of: pypdf, pymupdf.")
