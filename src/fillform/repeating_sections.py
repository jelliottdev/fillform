"""Helpers for repeating-section fill and validation.

A *repeating section* is a variable-length list of rows that maps to a fixed
grid of AcroForm fields.  Common examples in bankruptcy forms:

- Schedule D/E/F  — creditor rows (name, amount, account, priority)
- Schedule I      — monthly income sources
- Schedule J      — monthly expense categories
- B-104           — continuation sheet for overflow rows

This module provides:

``RepeatingSectionExpander``
    Expands ``FillPayload.repeating_values`` into flat ``{field_name: value}``
    entries that the fill engine can write directly.

``detect_repeating_slots``
    Heuristic that inspects a list of PDF widget names and groups them into
    likely repeating-section slot grids (useful when building a schema from
    scratch and you do not yet have ``RepeatingSection`` definitions).

``OverflowResult``
    Carries rows that could not fit onto the current form, along with the
    recommended continuation form family.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import FillPayload, RepeatingSection, CanonicalSchema


# ---------------------------------------------------------------------------
# Overflow result
# ---------------------------------------------------------------------------

@dataclass
class OverflowResult:
    """Rows that exceeded ``RepeatingSection.max_rows`` for one section."""

    section_id: str
    section_label: str
    overflow_rows: list[dict[str, Any]]   # Full row dicts for rows that didn't fit
    continuation_form: str | None         # Suggested next form family (may be None)

    @property
    def count(self) -> int:
        return len(self.overflow_rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "section_label": self.section_label,
            "overflow_row_count": self.count,
            "continuation_form": self.continuation_form,
            "overflow_rows": self.overflow_rows,
        }


# ---------------------------------------------------------------------------
# Expansion result
# ---------------------------------------------------------------------------

@dataclass
class ExpansionResult:
    """Output of :class:`RepeatingSectionExpander`."""

    # Flat dict ready for the fill engine: {pdf_field_name: value}
    flat_values: dict[str, Any] = field(default_factory=dict)
    # Rows that could not fit (one entry per overflowing section)
    overflow: list[OverflowResult] = field(default_factory=list)
    # Sections that had too few rows vs. min_rows
    undersized: list[dict[str, Any]] = field(default_factory=list)
    # Per-section stats: {section_id: {"rows_written": n, "rows_provided": m}}
    stats: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def has_overflow(self) -> bool:
        return bool(self.overflow)

    @property
    def has_undersize(self) -> bool:
        return bool(self.undersized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "flat_value_count": len(self.flat_values),
            "overflow": [o.to_dict() for o in self.overflow],
            "undersized": list(self.undersized),
            "stats": dict(self.stats),
        }


# ---------------------------------------------------------------------------
# Expander
# ---------------------------------------------------------------------------

class RepeatingSectionExpander:
    """Expands repeating-section row data into a flat field → value mapping.

    Usage::

        expander = RepeatingSectionExpander()
        result = expander.expand(schema, payload)
        # result.flat_values can be merged with payload.values for fill_engine
        if result.has_overflow:
            # warn attorney: continuation form needed
            ...
    """

    def expand(
        self,
        schema: CanonicalSchema,
        payload: FillPayload,
    ) -> ExpansionResult:
        """Expand all repeating-section rows in *payload* into flat field values.

        For each :class:`~fillform.contracts.RepeatingSection` declared in
        *schema*, this method:

        1. Reads the corresponding row list from ``payload.repeating_values``.
        2. Writes rows up to ``max_rows`` (or all rows if unlimited).
        3. Records any overflow rows in :attr:`ExpansionResult.overflow`.
        4. Records sections below ``min_rows`` in
           :attr:`ExpansionResult.undersized`.
        """
        result = ExpansionResult()

        for section in schema.repeating_sections:
            rows: list[dict[str, Any]] = list(
                payload.repeating_values.get(section.section_id) or []
            )
            rows_provided = len(rows)

            # Determine how many rows fit on this page
            max_rows = section.max_rows  # None = unlimited
            if max_rows is not None and rows_provided > max_rows:
                overflow_rows = rows[max_rows:]
                rows = rows[:max_rows]
                result.overflow.append(OverflowResult(
                    section_id=section.section_id,
                    section_label=section.label,
                    overflow_rows=overflow_rows,
                    continuation_form=section.continuation_form,
                ))

            rows_written = 0
            for row_idx, row_data in enumerate(rows):
                if not isinstance(row_data, dict):
                    continue
                for sec_field in section.fields:
                    value = row_data.get(sec_field.local_alias)
                    if value is None:
                        continue
                    pdf_name = sec_field.pdf_field_name(row_idx)
                    result.flat_values[pdf_name] = value
                rows_written += 1

            # Undersized check
            if section.min_rows > 0 and rows_written < section.min_rows:
                result.undersized.append({
                    "section_id": section.section_id,
                    "section_label": section.label,
                    "min_rows": section.min_rows,
                    "rows_provided": rows_provided,
                    "rows_written": rows_written,
                })

            result.stats[section.section_id] = {
                "rows_provided": rows_provided,
                "rows_written": rows_written,
            }

        return result


# ---------------------------------------------------------------------------
# Heuristic slot detector
# ---------------------------------------------------------------------------

@dataclass
class SlotGroup:
    """A detected repeating group of PDF field names."""

    base_name: str              # e.g. "creditor_name" (without index)
    template: str               # e.g. "creditor_{row}_name"
    slots: list[str]            # All field names in this group, ordered by row
    row_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_name": self.base_name,
            "template": self.template,
            "slots": self.slots,
            "row_count": self.row_count,
        }


def detect_repeating_slots(
    field_names: list[str],
    min_repetitions: int = 2,
) -> list[SlotGroup]:
    """Heuristic: group PDF field names that look like a repeating grid.

    Detects patterns like:
    - ``creditor_1_name``, ``creditor_2_name``, ...
    - ``Row1.Name``, ``Row2.Name``, ...
    - ``creditor[0].name``, ``creditor[1].name``, ...
    - ``NameLine1``, ``NameLine2``, ``NameLine3``

    Returns :class:`SlotGroup` instances, one per detected column in the
    repeating grid.  Groups with fewer than *min_repetitions* members are
    excluded.
    """

    # Patterns that match an embedded integer: capture prefix, index, suffix
    _PATTERNS = [
        re.compile(r"^(.*?)(\d+)(.*)$"),           # any embedded digit sequence
    ]

    # Group field names by (prefix, suffix) — those with the same prefix/suffix
    # but different numeric indexes are candidates for a repeating section.
    groups: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for name in field_names:
        for pat in _PATTERNS:
            m = pat.match(name)
            if m:
                prefix, idx_str, suffix = m.group(1), m.group(2), m.group(3)
                key = (prefix, suffix)
                groups.setdefault(key, []).append((int(idx_str), name))
                break

    result: list[SlotGroup] = []
    for (prefix, suffix), entries in groups.items():
        if len(entries) < min_repetitions:
            continue
        entries.sort(key=lambda e: e[0])
        slots = [e[1] for e in entries]
        # Build a template by replacing the first numeric index with {row}
        template = f"{prefix}{{row}}{suffix}"
        # Use a normalized base_name without the index
        base_name = f"{prefix.rstrip('_').rstrip('.')}{suffix.lstrip('_').lstrip('.')}"
        result.append(SlotGroup(
            base_name=base_name or f"{prefix}{suffix}",
            template=template,
            slots=slots,
            row_count=len(slots),
        ))

    # De-duplicate: if a longer prefix already covers a shorter one, keep longer
    result.sort(key=lambda g: -len(g.base_name))
    seen_slots: set[str] = set()
    deduped: list[SlotGroup] = []
    for grp in result:
        new_slots = [s for s in grp.slots if s not in seen_slots]
        if len(new_slots) >= min_repetitions:
            seen_slots.update(new_slots)
            deduped.append(SlotGroup(
                base_name=grp.base_name,
                template=grp.template,
                slots=new_slots,
                row_count=len(new_slots),
            ))

    return deduped


# ---------------------------------------------------------------------------
# Continuation page planner
# ---------------------------------------------------------------------------

@dataclass
class ContinuationPlan:
    """Describes how overflow rows should be distributed across continuation forms."""

    section_id: str
    section_label: str
    pages: list[dict[str, Any]]   # [{form_family, rows_start, rows_end, row_data}]
    continuation_form: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "section_label": self.section_label,
            "continuation_form": self.continuation_form,
            "page_count": len(self.pages),
            "pages": self.pages,
        }


def plan_continuations(
    overflow: list[OverflowResult],
    rows_per_continuation: int = 15,
) -> list[ContinuationPlan]:
    """Split overflow rows across continuation pages.

    Parameters
    ----------
    overflow:
        List of :class:`OverflowResult` instances from
        :class:`RepeatingSectionExpander`.
    rows_per_continuation:
        How many rows fit on each continuation sheet (default 15).

    Returns a :class:`ContinuationPlan` per overflowing section.
    """
    plans: list[ContinuationPlan] = []
    for ov in overflow:
        pages: list[dict[str, Any]] = []
        rows = ov.overflow_rows
        i = 0
        page_num = 1
        while i < len(rows):
            chunk = rows[i: i + rows_per_continuation]
            pages.append({
                "page_number": page_num,
                "form_family": ov.continuation_form,
                "rows_start": i,
                "rows_end": i + len(chunk) - 1,
                "row_count": len(chunk),
                "row_data": chunk,
            })
            i += rows_per_continuation
            page_num += 1
        plans.append(ContinuationPlan(
            section_id=ov.section_id,
            section_label=ov.section_label,
            pages=pages,
            continuation_form=ov.continuation_form,
        ))
    return plans
