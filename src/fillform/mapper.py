"""Semantic mapping orchestration.

Delegates to :class:`~fillform.vision_mapper.VisionFieldMapper` for the
multi-pass Claude vision analysis that turns raw widget geometry into a fully
annotated :class:`~fillform.contracts.CanonicalSchema`.

If vision analysis is not available (missing API key or annotated PDF), the
mapper falls back to geometry-only label inference using nearby text extraction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import CanonicalField, CanonicalSchema
from .structure import StructuralRepresentation


class SemanticMapper:
    """Staged mapper: annotate → vision analysis → confidence fusion → schema."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-opus-4-6",
    ) -> None:
        self._api_key = api_key
        self._model = model

    def map_to_schema(
        self,
        structure: StructuralRepresentation,
        form_family: str,
        version: str,
        annotated_pdf: str | Path | None = None,
        vision_passes: int = 2,
        vision_dpi: int = 150,
    ) -> CanonicalSchema:
        """Produce a :class:`CanonicalSchema` from *structure*.

        When *annotated_pdf* is supplied the full vision pipeline runs.
        Otherwise the mapper uses geometry + nearby-text label inference only.

        Parameters
        ----------
        structure:
            Structural representation extracted by
            :class:`~fillform.structure.PdfStructureService`.
        form_family:
            Logical form name stored in the returned schema.
        version:
            Schema version string.
        annotated_pdf:
            Path to the annotated PDF (orange FXXX labels).  When provided,
            multi-pass Claude vision analysis populates all semantic fields.
        vision_passes:
            Number of Claude vision passes per page (1 or 2).
        vision_dpi:
            Resolution used when rendering pages for vision analysis.
        """
        if annotated_pdf is not None:
            return self._vision_map(
                structure=structure,
                form_family=form_family,
                version=version,
                annotated_pdf=Path(annotated_pdf),
                passes=vision_passes,
                dpi=vision_dpi,
            )

        return self._geometry_map(structure=structure, form_family=form_family, version=version)

    # ------------------------------------------------------------------
    # Vision path
    # ------------------------------------------------------------------

    def _vision_map(
        self,
        structure: StructuralRepresentation,
        form_family: str,
        version: str,
        annotated_pdf: Path,
        passes: int,
        dpi: int,
    ) -> CanonicalSchema:
        from .field_alias import FieldAliasRegistry
        from .vision_mapper import VisionFieldMapper

        alias_registry = FieldAliasRegistry()
        alias_map = alias_registry.assign(structure.field_widgets)

        mapper = VisionFieldMapper(api_key=self._api_key, model=self._model)
        return mapper.map_fields(
            annotated_pdf=annotated_pdf,
            alias_map=alias_map,
            form_family=form_family,
            version=version,
            passes=passes,
            dpi=dpi,
        )

    # ------------------------------------------------------------------
    # Geometry-only fallback path
    # ------------------------------------------------------------------

    def _geometry_map(
        self,
        structure: StructuralRepresentation,
        form_family: str,
        version: str,
    ) -> CanonicalSchema:
        """Build a minimal schema using only widget geometry and nearby text."""
        from .field_alias import FieldAliasRegistry

        alias_registry = FieldAliasRegistry()
        alias_map = alias_registry.assign(structure.field_widgets)

        fields: list[CanonicalField] = []
        for alias, widget in sorted(alias_map.field_widgets.items()):
            label = self._infer_label(widget, structure)
            fields.append(
                CanonicalField(
                    alias=alias,
                    field_name=widget.name,
                    field_type=widget.field_type,
                    page=widget.page,
                    bbox=widget.bbox,
                    label=label,
                    context=None,
                    expected_value_type=_type_from_field_type(widget.field_type),
                    expected_format=None,
                    is_required=False,
                    section=None,
                )
            )

        return CanonicalSchema(
            form_family=form_family,
            version=version,
            mode="acroform",
            fields=fields,
        )

    def _infer_label(self, widget: Any, structure: StructuralRepresentation) -> str | None:
        """Return the best available label for a widget from nearby text."""
        # Use the raw field name as a readable fallback
        raw = str(getattr(widget, "name", "") or "")
        if not raw:
            return None

        # Strip common PDF naming noise
        for sep in (".", "_", "-"):
            raw = raw.replace(sep, " ")
        return raw.strip() or None


def _type_from_field_type(field_type: str) -> str:
    ft = (field_type or "").lower()
    if ft in {"btn", "button"}:
        return "boolean"
    if ft in {"ch", "choice"}:
        return "selection"
    if ft in {"sig", "signature"}:
        return "signature"
    return "string"
