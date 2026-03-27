"""Tool-facing API surface for agents (MCP-oriented service layer)."""

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
        output_pdf: str | Path | None = None,
    ) -> FillResult:
        """Fill *source_pdf* using *schema* + *payload*.

        Parameters
        ----------
        output_pdf:
            Destination path for the filled PDF.  When omitted the fill engine
            writes to a temporary file.
        """
        return self.fill_engine.fill(source_pdf, schema, payload, output_pdf=output_pdf)

    def verify_form(
        self,
        payload: FillPayload,
        schema: CanonicalSchema | None = None,
        filled_pdf: str | Path | None = None,
    ) -> VerificationReport:
        """Run multi-layer verification over a completed fill.

        Parameters
        ----------
        schema:
            Canonical schema used to check required-field coverage and formats.
        filled_pdf:
            Path to the output PDF for readback verification.
        """
        return self.verification.verify(payload=payload, schema=schema, filled_pdf=filled_pdf)

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
            tmp = tempfile.NamedTemporaryFile(suffix="_annotated.pdf", delete=False)
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

        # 5. Cache in registry
        self.registry.put(schema)

        return schema, alias_map, annotated_path

    # ------------------------------------------------------------------
    # Convenience: analyze + fill + verify in one call
    # ------------------------------------------------------------------

    def analyze_fill_verify(
        self,
        pdf_path: str | Path,
        payload: FillPayload,
        form_family: str = "unknown",
        version: str = "1",
        output_pdf: str | Path | None = None,
        vision_passes: int = 2,
        vision_dpi: int = 150,
    ) -> tuple[CanonicalSchema, FillResult, VerificationReport]:
        """Full pipeline: analyze → fill → verify.

        Returns
        -------
        (schema, fill_result, verification_report)
        """
        schema, _alias_map, annotated_path = self.analyze_form(
            pdf_path=pdf_path,
            form_family=form_family,
            version=version,
            vision_passes=vision_passes,
            vision_dpi=vision_dpi,
        )
        try:
            annotated_path.unlink(missing_ok=True)
        except Exception:
            pass

        fill_result = self.fill_form(
            source_pdf=pdf_path,
            schema=schema,
            payload=payload,
            output_pdf=output_pdf,
        )

        report = self.verify_form(
            payload=payload,
            schema=schema,
            filled_pdf=fill_result.flattened_pdf_path,
        )

        return schema, fill_result, report
