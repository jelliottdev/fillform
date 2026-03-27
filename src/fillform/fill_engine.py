"""Deterministic fill engine — AcroForm implementation using PyMuPDF.

Pipeline
--------
1. Open the source PDF with fitz.
2. Index all AcroForm widgets by field_name.
3. Resolve each payload key (alias or raw field_name) via the schema alias map.
4. Write each value using the appropriate strategy:
   - Text / choice fields  → widget.field_value = str(value)
   - Single checkbox       → resolve the correct on-state string via button_states()
   - Radio / checkbox group (multiple widgets sharing a name) → select exactly one
5. Save the output PDF and return a FillResult with a deterministic write-action log.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import CanonicalSchema, FillPayload, FillWriteAction
from .repeating_sections import ExpansionResult, RepeatingSectionExpander


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FillResult:
    draft_pdf_path: Path
    flattened_pdf_path: Path
    fill_log: dict[str, str]
    write_actions: list[FillWriteAction] = field(default_factory=list)
    changed_fields: list[dict[str, str]] = field(default_factory=list)
    repeating_expansion: ExpansionResult | None = None


# ---------------------------------------------------------------------------
# Widget helpers
# ---------------------------------------------------------------------------

def _is_checkbox_widget(widget: Any) -> bool:
    fts = str(getattr(widget, "field_type_string", "")).lower()
    return "checkbox" in fts or "button" in fts or str(getattr(widget, "field_type", "")) == "2"


def _checkbox_on_state(widget: Any) -> str:
    """Return the non-Off export value for a checkbox/radio widget."""
    try:
        if hasattr(widget, "on_state"):
            return str(widget.on_state())
    except Exception:
        pass
    try:
        states = widget.button_states() or {}
        normal = states.get("normal") or []
        for state in normal:
            s = str(state)
            if s.lower() != "off":
                return s
    except Exception:
        pass
    return "Yes"


def _coerce_for_widget(widget: Any, raw_value: Any) -> str:
    """Convert a raw Python value to the string the PDF widget expects."""
    if isinstance(raw_value, bool) and _is_checkbox_widget(widget):
        return _checkbox_on_state(widget) if raw_value else "Off"
    return "" if raw_value is None else str(raw_value)


def _normalize_bool_value(value: Any) -> str | None:
    """Normalize yes/no/true/false variants.  Returns 'yes', 'no', or None."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value or "").strip().lower()
    if text in {"yes", "true", "on", "1"}:
        return "yes"
    if text in {"no", "false", "off", "0"}:
        return "no"
    return None


def _pick_widget_for_value(widgets: list[Any], raw_value: Any) -> int:
    """Return the index of the widget that should be set to its on-state.

    Returns -1 to indicate all widgets should be set to Off (i.e. deselect).
    """
    normalized = _normalize_bool_value(raw_value)

    # Explicit yes → pick the first widget whose on-state contains "yes" or "true"
    if normalized == "yes":
        for idx, w in enumerate(widgets):
            name = str(getattr(w, "field_name", "")).lower()
            on = _checkbox_on_state(w).lower()
            if "yes" in on or on in {"1", "on", "true"} or "yes" in name:
                return idx
        return 0  # fallback: first widget

    if normalized == "no":
        for idx, w in enumerate(widgets):
            name = str(getattr(w, "field_name", "")).lower()
            on = _checkbox_on_state(w).lower()
            if "no" in on or on in {"0", "off", "false"} or "no" in name:
                return idx
        return -1  # deselect all

    # Exact state match
    raw_str = str(raw_value).strip().lower()
    for idx, w in enumerate(widgets):
        if _checkbox_on_state(w).strip().lower() == raw_str:
            return idx

    return -1  # unknown value → deselect


def _fill_widget_group(
    widgets: list[Any],
    raw_value: Any,
    field_name: str,
    changed_fields: list[dict[str, str]],
) -> str:
    """Apply *raw_value* to a group of widgets sharing the same field_name."""
    target_idx = _pick_widget_for_value(widgets, raw_value)
    try:
        for idx, widget in enumerate(widgets):
            before = str(widget.field_value or "")
            after_value = _checkbox_on_state(widget) if idx == target_idx else "Off"
            widget.field_value = after_value
            widget.update()
            after = str(widget.field_value or "")
            if before != after:
                changed_fields.append({
                    "field_name": f"{field_name}[{idx}]",
                    "before": before,
                    "after": after,
                })
    except Exception as exc:
        return f"error:{field_name}:{exc}"
    return f"ok:{field_name}"


# ---------------------------------------------------------------------------
# Checksum helpers (short hashes for audit log — not security-critical)
# ---------------------------------------------------------------------------

def _value_checksum(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def _file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# FillEngine
# ---------------------------------------------------------------------------

class FillEngine:
    """Writes AcroForm field values into a PDF and returns a deterministic audit log."""

    def fill(
        self,
        source_pdf: str | Path,
        schema: CanonicalSchema,
        payload: FillPayload,
        output_pdf: str | Path | None = None,
    ) -> FillResult:
        """Fill *source_pdf* according to *schema* + *payload*.

        Parameters
        ----------
        source_pdf:
            Path to the AcroForm PDF to fill.
        schema:
            Canonical schema describing every field.  The alias→field_name map
            is derived from this schema so callers can use FXXX keys.
        payload:
            Fill values keyed by either FXXX alias or raw PDF field name.
        output_pdf:
            Destination path for the filled PDF.  Defaults to a temporary file.

        Returns
        -------
        FillResult with output paths, per-key fill_log, write_actions, and
        the list of fields whose values actually changed.
        """
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError(
                "PyMuPDF (fitz) is required for fill operations. "
                "Install it with: pip install pymupdf"
            ) from exc

        source = Path(source_pdf)

        if output_pdf is None:
            tmp = tempfile.NamedTemporaryFile(suffix="_filled.pdf", delete=False)
            tmp.close()
            out_path = Path(tmp.name)
        else:
            out_path = Path(output_pdf)

        # Build alias → field_name from schema
        alias_to_field: dict[str, str] = {f.alias: f.field_name for f in schema.fields}

        # Expand repeating-section rows into flat field → value pairs
        expansion: ExpansionResult | None = None
        extra_flat: dict[str, Any] = {}
        if schema.repeating_sections and payload.repeating_values:
            expansion = RepeatingSectionExpander().expand(schema, payload)
            extra_flat = expansion.flat_values

        fill_log: dict[str, str] = {}
        changed_fields: list[dict[str, str]] = []
        write_actions: list[FillWriteAction] = []
        seq = 0

        with fitz.open(str(source)) as doc:
            # Index all widgets by field_name
            widgets_by_name: dict[str, list[Any]] = {}
            for page in doc:
                for widget in page.widgets() or []:
                    if widget.field_name:
                        widgets_by_name.setdefault(str(widget.field_name), []).append(widget)

            # Resolve alias/raw keys to canonical field names
            resolved: list[tuple[str, str, Any]] = []
            for key, raw_value in payload.values.items():
                field_name = alias_to_field.get(str(key), str(key))
                resolved.append((str(key), field_name, raw_value))
            # Append expanded repeating-section entries (already use PDF field names)
            for pdf_field_name, raw_value in extra_flat.items():
                resolved.append((pdf_field_name, pdf_field_name, raw_value))

            for key_str, field_name, raw_value in resolved:
                widgets = widgets_by_name.get(field_name) or []

                if not widgets:
                    fill_log[key_str] = f"missing_field:{field_name}"
                    seq += 1
                    write_actions.append(FillWriteAction(
                        sequence=seq,
                        action="skip",
                        target=field_name,
                        metadata={"reason": "field_not_found", "key": key_str},
                    ))
                    continue

                try:
                    if len(widgets) > 1:
                        # Radio button / checkbox group
                        status = _fill_widget_group(
                            widgets=widgets,
                            raw_value=raw_value,
                            field_name=field_name,
                            changed_fields=changed_fields,
                        )
                        # Annotate changed_fields entries with the input key
                        for entry in changed_fields:
                            if "input_key" not in entry:
                                entry["input_key"] = key_str
                    else:
                        widget = widgets[0]
                        value = _coerce_for_widget(widget, raw_value)
                        before = str(widget.field_value or "")
                        widget.field_value = value
                        widget.update()
                        after = str(widget.field_value or "")
                        if before != after:
                            changed_fields.append({
                                "input_key": key_str,
                                "field_name": field_name,
                                "before": before,
                                "after": after,
                            })
                        status = f"ok:{field_name}"

                    fill_log[key_str] = status
                    seq += 1
                    write_actions.append(FillWriteAction(
                        sequence=seq,
                        action="write",
                        target=field_name,
                        payload_checksum=_value_checksum(raw_value),
                        metadata={"key": key_str, "status": status},
                    ))

                except Exception as exc:
                    err_status = f"error:{field_name}:{exc}"
                    fill_log[key_str] = err_status
                    seq += 1
                    write_actions.append(FillWriteAction(
                        sequence=seq,
                        action="error",
                        target=field_name,
                        metadata={"key": key_str, "error": str(exc)},
                    ))

            out_path.parent.mkdir(parents=True, exist_ok=True)
            doc.save(str(out_path))

        return FillResult(
            draft_pdf_path=out_path,
            flattened_pdf_path=out_path,
            fill_log=fill_log,
            write_actions=write_actions,
            changed_fields=changed_fields,
            repeating_expansion=expansion,
        )
