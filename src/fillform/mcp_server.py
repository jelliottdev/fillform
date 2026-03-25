"""Tool-facing API surface for agents (MCP-oriented skeleton)."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .annotator import PdfAnnotator
from .contracts import CanonicalSchema, FillPayload, VerificationReport
from .field_alias import AliasMap, FieldAliasRegistry
from .fill_engine import FillEngine, FillResult
from .ingest import IngestionService
from .schema_registry import SchemaRegistry
from .structure import PdfStructureService
from .verify import VerificationEngine
from .vision_mapper import VisionFieldMapper


@dataclass(slots=True)
class FillFormService:
    ingestion: IngestionService
    registry: SchemaRegistry
    fill_engine: FillEngine
    verification: VerificationEngine
    structure_service: PdfStructureService = field(default_factory=PdfStructureService)
    alias_registry: FieldAliasRegistry = field(default_factory=FieldAliasRegistry)
    annotator: PdfAnnotator = field(default_factory=PdfAnnotator)
    vision_mapper: VisionFieldMapper = field(default_factory=VisionFieldMapper)

    def upload_form(self, pdf_path: str | Path, document_id: str):
        return self.ingestion.ingest(pdf_path, document_id)

    def get_schema(self, form_family: str, version: str) -> CanonicalSchema | None:
        return self.registry.get(form_family, version)

    def fill_form(
        self,
        source_pdf: str | Path,
        schema: CanonicalSchema,
        payload: FillPayload,
    ) -> FillResult:
        return self.fill_engine.fill(source_pdf, schema, payload)

    def verify_form(self, payload: FillPayload) -> VerificationReport:
        return self.verification.verify(payload)

    # ------------------------------------------------------------------
    # Field-mapping pipeline
    # ------------------------------------------------------------------

    def analyze_form(
        self,
        pdf_path: str | Path,
        form_family: str = "unknown",
        version: str = "1",
        vision_passes: int = 2,
        vision_dpi: int = 150,
        annotated_output: str | Path | None = None,
    ) -> tuple[CanonicalSchema, AliasMap, Path]:
        """Run the full field-mapping pipeline on an AcroForm PDF.

        Steps
        -----
        1. Extract AcroForm widgets (page, bbox, field type) via the structure service.
        2. Assign sequential FXXX aliases to every unique field.
        3. Render an annotated copy of the PDF with vibrant orange FXXX overlays.
        4. Pass each annotated page to a Claude vision model (multi-pass) to identify
           what each field is for, its expected value type and format, and whether it
           is required.
        5. Return the populated :class:`~fillform.contracts.CanonicalSchema`,
           the raw :class:`~fillform.field_alias.AliasMap`, and the path to the
           annotated PDF.

        Parameters
        ----------
        pdf_path:
            Path to the source PDF.
        form_family:
            Logical form family name (used in the schema and fill script).
        version:
            Schema version string.
        vision_passes:
            Number of vision-model passes per page.  2 is recommended.
        vision_dpi:
            Image resolution for vision rendering.  150 is a good default.
        annotated_output:
            Where to write the annotated PDF.  Defaults to a temp file.

        Returns
        -------
        (schema, alias_map, annotated_pdf_path)
        """
        source = Path(pdf_path)

        # 1. Extract widgets
        structure = self.structure_service.extract(source)

        if not structure.field_widgets:
            raise ValueError(
                f"No AcroForm fields found in '{source}'. "
                "Only AcroForm PDFs are supported by this pipeline."
            )

        # 2. Assign FXXX aliases
        alias_map = self.alias_registry.assign(structure.field_widgets)

        # 3. Annotate PDF
        if annotated_output is None:
            tmp = tempfile.NamedTemporaryFile(
                suffix="_annotated.pdf", delete=False
            )
            tmp.close()
            annotated_path = Path(tmp.name)
        else:
            annotated_path = Path(annotated_output)

        self.annotator.annotate(source, alias_map, annotated_path)

        # 4. Vision analysis
        schema = self.vision_mapper.map_fields(
            annotated_pdf=annotated_path,
            alias_map=alias_map,
            form_family=form_family,
            version=version,
            passes=vision_passes,
            dpi=vision_dpi,
        )

        return schema, alias_map, annotated_path
