"""AcroForm field alias assignment: maps raw field names to sequential FXXX identifiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .structure import FieldWidget


@dataclass(frozen=True)
class AliasMap:
    """Bidirectional mapping between PDF field names and FXXX aliases.

    Fields are assigned aliases in reading order (page → top-to-bottom → left-to-right).
    The JSON representation is the key mapping an external agent or user sees:
    ``{"first_name": "F001", "last_name": "F002", ...}``
    """

    alias_to_field: dict[str, str]       # F001 -> raw_field_name
    field_to_alias: dict[str, str]       # raw_field_name -> F001
    field_widgets: dict[str, FieldWidget]  # alias -> FieldWidget (position data)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the key-mapping JSON.

        Returns a dict with two sub-keys:
        - ``key_mapping``: ``{raw_field_name: alias}`` — intended for human review
        - ``alias_index``: ``{alias: raw_field_name}`` — intended for reverse lookup
        """
        return {
            "key_mapping": dict(self.field_to_alias),
            "alias_index": dict(self.alias_to_field),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], widgets: Sequence[FieldWidget] | None = None) -> "AliasMap":
        key_mapping: dict[str, str] = dict(payload.get("key_mapping") or {})
        alias_index: dict[str, str] = dict(payload.get("alias_index") or {})

        widget_lookup: dict[str, FieldWidget] = {}
        if widgets:
            name_to_widget = {w.name: w for w in widgets}
            for alias, field_name in alias_index.items():
                w = name_to_widget.get(field_name)
                if w:
                    widget_lookup[alias] = w

        return cls(
            alias_to_field=alias_index,
            field_to_alias=key_mapping,
            field_widgets=widget_lookup,
        )


class FieldAliasRegistry:
    """Assigns sequential FXXX aliases to AcroForm field widgets.

    Ordering is deterministic: fields are sorted by page number, then by
    vertical position (top to bottom), then by horizontal position (left to
    right).  Duplicate field names (shared across pages) receive a single alias
    from their first occurrence.
    """

    def assign(self, widgets: Sequence[FieldWidget]) -> AliasMap:
        """Return an :class:`AliasMap` covering every unique field in *widgets*."""
        # Sort for consistent reading order
        sorted_widgets = sorted(
            widgets,
            key=lambda w: (w.page, -w.bbox[3], w.bbox[0]),  # page, -y1 (top first), x0
        )

        alias_to_field: dict[str, str] = {}
        field_to_alias: dict[str, str] = {}
        alias_to_widget: dict[str, FieldWidget] = {}

        seen: set[str] = set()
        counter = 1

        for widget in sorted_widgets:
            if widget.name in seen:
                continue
            seen.add(widget.name)

            alias = f"F{counter:03d}"
            alias_to_field[alias] = widget.name
            field_to_alias[widget.name] = alias
            alias_to_widget[alias] = widget
            counter += 1

        return AliasMap(
            alias_to_field=alias_to_field,
            field_to_alias=field_to_alias,
            field_widgets=alias_to_widget,
        )
