"""Multi-pass vision analysis for mapping FXXX aliases to semantic field definitions.

Pipeline
--------
1. The annotated PDF (with orange FXXX labels) is rendered page-by-page to PNG.
2. Each page image is sent to a Claude vision model in **two passes**:
   - Pass 1 — broad extraction: identify every labeled field and describe its purpose.
   - Pass 2 — verification & enrichment: review pass-1 output, correct errors, fill gaps.
3. Responses are merged into a list of :class:`~fillform.contracts.CanonicalField`
   objects that form the final :class:`~fillform.contracts.CanonicalSchema`.

The resulting schema's ``to_fill_script()`` method produces a complete AI fill guide.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from .contracts import CanonicalField, CanonicalSchema
from .field_alias import AliasMap

_DEFAULT_MODEL = "claude-opus-4-6"
_DEFAULT_DPI = 150
_DEFAULT_PASSES = 2

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_PASS1_SYSTEM = (
    "You are an expert form analyst with deep knowledge of legal, government, "
    "medical, financial, and HR documents. You will be shown a PDF form page "
    "where each AcroForm field has been highlighted with a vibrant orange label "
    "(F001, F002, …). Analyse the surrounding text, headers, and layout to "
    "determine exactly what information each labeled field is asking for."
)

_PASS1_USER = """\
The following field aliases appear on this page: {alias_list}.

For EACH alias visible on the page, provide a structured JSON object. \
Return ONLY the JSON — no markdown fences, no explanation text.

Required JSON structure:
{{
  "F001": {{
    "label": "Short human-readable label (e.g. 'First Name', 'Date of Birth')",
    "context": "One sentence describing what information goes here",
    "expected_value_type": "string | date | number | boolean | signature | selection",
    "expected_format": "Format hint or null  (e.g. 'MM/DD/YYYY', 'SSN: XXX-XX-XXXX')",
    "is_required": true,
    "section": "Section or group name from the form, or null"
  }},
  ...
}}
"""

_PASS2_SYSTEM = (
    "You are verifying a form-field semantic mapping. You will be shown the same "
    "form page image and the mapping produced by a first analysis pass. Your job "
    "is to correct mistakes, add missing aliases, and sharpen ambiguous descriptions."
)

_PASS2_USER = """\
Here is the pass-1 field mapping:

{previous_mapping}

Review the form image carefully and return the corrected, complete mapping for \
aliases {alias_list}. Apply these rules:
- Every alias visible on the page must appear in the output.
- If a field label or purpose is unclear, make the best inference from context.
- Do not remove fields that were correctly identified in pass 1.
- Return ONLY the corrected JSON — same structure as above — no other text.
"""


class VisionFieldMapper:
    """Maps FXXX aliases to semantic field definitions via multi-pass Claude vision.

    Parameters
    ----------
    api_key:
        Anthropic API key.  Falls back to the ``ANTHROPIC_API_KEY`` environment
        variable if not supplied.
    model:
        Claude model ID to use for vision inference.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
    ) -> None:
        self._api_key = api_key
        self._model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def map_fields(
        self,
        annotated_pdf: str | Path,
        alias_map: AliasMap,
        form_family: str = "unknown",
        version: str = "1",
        passes: int = _DEFAULT_PASSES,
        dpi: int = _DEFAULT_DPI,
    ) -> CanonicalSchema:
        """Analyse *annotated_pdf* and return a fully-populated :class:`CanonicalSchema`.

        Parameters
        ----------
        annotated_pdf:
            Path to the annotated PDF produced by :class:`~fillform.annotator.PdfAnnotator`.
        alias_map:
            The alias map produced for this PDF by :class:`~fillform.field_alias.FieldAliasRegistry`.
        form_family:
            Logical name for this form type (used in the schema and fill script).
        version:
            Schema version string.
        passes:
            Number of vision analysis passes per page (1 or 2).  Two passes
            substantially improve accuracy on complex forms.
        dpi:
            Resolution for rendering PDF pages to images.  150 dpi balances
            quality and token cost; increase to 200 for small or dense text.
        """
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "The 'anthropic' package is required for vision mapping. "
                "Install it with: pip install anthropic"
            ) from exc

        client = (
            anthropic.Anthropic(api_key=self._api_key)
            if self._api_key
            else anthropic.Anthropic()
        )

        page_images = self._render_pages(Path(annotated_pdf), dpi=dpi)

        all_field_data: dict[str, dict[str, Any]] = {}

        for page_index, image_b64 in enumerate(page_images):
            page_aliases = {
                alias: widget
                for alias, widget in alias_map.field_widgets.items()
                if widget.page == page_index
            }
            if not page_aliases:
                continue

            page_data = self._analyse_page(
                client=client,
                image_b64=image_b64,
                page_aliases=page_aliases,
                passes=passes,
            )
            all_field_data.update(page_data)

        canonical_fields = self._build_canonical_fields(alias_map, all_field_data)

        return CanonicalSchema(
            form_family=form_family,
            version=version,
            mode="acroform",
            fields=canonical_fields,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _render_pages(self, pdf_path: Path, dpi: int) -> list[str]:
        """Render each PDF page to a base64-encoded PNG string."""
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError(
                "PyMuPDF (fitz) is required for page rendering. "
                "Install it with: pip install pymupdf"
            ) from exc

        images: list[str] = []
        scale = dpi / 72.0
        mat = fitz.Matrix(scale, scale)

        with fitz.open(str(pdf_path)) as doc:
            for page_index in range(doc.page_count):
                page = doc.load_page(page_index)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                png_bytes = pix.tobytes("png")
                images.append(base64.standard_b64encode(png_bytes).decode())

        return images

    def _analyse_page(
        self,
        client: Any,
        image_b64: str,
        page_aliases: dict[str, Any],
        passes: int,
    ) -> dict[str, dict[str, Any]]:
        """Run one or two vision passes on a single page image."""
        alias_list = ", ".join(sorted(page_aliases.keys()))
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": image_b64,
            },
        }

        # ── Pass 1 ────────────────────────────────────────────────────
        response1 = client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=_PASS1_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        image_block,
                        {
                            "type": "text",
                            "text": _PASS1_USER.format(alias_list=alias_list),
                        },
                    ],
                }
            ],
        )
        field_data = self._parse_json_response(response1.content[0].text)

        if passes < 2:
            return field_data

        # ── Pass 2 ────────────────────────────────────────────────────
        prev_json = json.dumps(field_data, indent=2)
        response2 = client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=_PASS2_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        image_block,
                        {
                            "type": "text",
                            "text": _PASS2_USER.format(
                                previous_mapping=prev_json,
                                alias_list=alias_list,
                            ),
                        },
                    ],
                }
            ],
        )
        field_data = self._parse_json_response(response2.content[0].text)

        return field_data

    def _parse_json_response(self, text: str) -> dict[str, Any]:
        """Extract a JSON object from model output, tolerating markdown fences."""
        text = text.strip()

        # Strip ```json ... ``` or ``` ... ``` fences
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence_match:
            text = fence_match.group(1).strip()

        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # Last-resort: find the first {...} block
        brace_match = re.search(r"\{[\s\S]*\}", text)
        if brace_match:
            try:
                result = json.loads(brace_match.group(0))
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        return {}

    def _build_canonical_fields(
        self,
        alias_map: AliasMap,
        field_data: dict[str, dict[str, Any]],
    ) -> list[CanonicalField]:
        """Combine alias map geometry with vision-analysis semantics."""
        fields: list[CanonicalField] = []

        for alias, widget in sorted(alias_map.field_widgets.items()):
            data = field_data.get(alias) or {}
            fields.append(
                CanonicalField(
                    alias=alias,
                    field_name=widget.name,
                    field_type=widget.field_type,
                    page=widget.page,
                    bbox=widget.bbox,
                    label=data.get("label"),
                    context=data.get("context"),
                    expected_value_type=data.get("expected_value_type"),
                    expected_format=data.get("expected_format"),
                    is_required=bool(data.get("is_required", False)),
                    section=data.get("section"),
                )
            )

        return fields
