"""Schema version diffing and migration planning.

When a court updates a PDF form, FillForm needs to:

  1. Detect what changed between the old and new schema versions.
  2. Map old FXXX aliases to new aliases where possible (so prior fill payloads
     can be reused without manually re-identifying every field).
  3. Produce a migration plan that tells callers exactly what to do:
     - which prior mappings are safe to carry forward
     - which fields need re-review because they changed semantically
     - which fields disappeared and need to be removed from payloads
     - which new required fields need to be filled for the first time

Typical usage
-------------
::

    old_schema = registry.get("B-22A", "2023")
    new_schema = analyze_form(new_pdf, form_family="B-22A", version="2024")
    diff = diff_schemas(old_schema, new_schema)
    print(diff.summary())

    plan = migration_plan(diff)
    for action in plan:
        print(action.kind, action.alias, "→", action.description)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import CanonicalField, CanonicalSchema


# ---------------------------------------------------------------------------
# Field-level diff
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldChange:
    """A single property change on a field that exists in both schemas."""

    attribute: str          # "label", "field_type", "expected_value_type", "is_required", etc.
    old_value: Any
    new_value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "attribute": self.attribute,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }


@dataclass(frozen=True)
class FieldDiff:
    """Comparison result for a single field that exists in both schema versions."""

    old_alias: str
    new_alias: str
    field_name: str
    match_method: str       # "exact_name" | "label_fuzzy" | "position_heuristic"
    match_confidence: float # 0.0 – 1.0
    changes: list[FieldChange] = field(default_factory=list)

    @property
    def is_changed(self) -> bool:
        return bool(self.changes)

    @property
    def is_safe_to_migrate(self) -> bool:
        """True when field_name and type are unchanged (value is still valid)."""
        changed_attrs = {c.attribute for c in self.changes}
        # Type or format change means the old value may no longer be valid
        unsafe = {"field_type", "expected_value_type", "expected_format"}
        return not (changed_attrs & unsafe)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_alias": self.old_alias,
            "new_alias": self.new_alias,
            "field_name": self.field_name,
            "match_method": self.match_method,
            "match_confidence": self.match_confidence,
            "is_changed": self.is_changed,
            "is_safe_to_migrate": self.is_safe_to_migrate,
            "changes": [c.to_dict() for c in self.changes],
        }


# ---------------------------------------------------------------------------
# Schema-level diff
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SchemaDiff:
    """Complete diff between two versions of the same form family."""

    old_version: str
    new_version: str
    form_family: str

    # Fields that exist in both versions (possibly changed)
    matched: list[FieldDiff] = field(default_factory=list)
    # Fields that exist only in the new schema
    added: list[CanonicalField] = field(default_factory=list)
    # Fields that exist only in the old schema
    removed: list[CanonicalField] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return sum(1 for m in self.matched if m.is_changed)

    @property
    def safe_migration_count(self) -> int:
        return sum(1 for m in self.matched if m.is_safe_to_migrate)

    @property
    def breaking_change_count(self) -> int:
        return sum(1 for m in self.matched if m.is_changed and not m.is_safe_to_migrate)

    def summary(self) -> str:
        lines = [
            f"Schema diff: {self.form_family}  v{self.old_version} → v{self.new_version}",
            f"  Matched  : {len(self.matched)} field(s) ({self.changed_count} changed, "
            f"{self.safe_migration_count} safe to migrate, "
            f"{self.breaking_change_count} breaking)",
            f"  Added    : {len(self.added)} new field(s)",
            f"  Removed  : {len(self.removed)} removed field(s)",
        ]
        if self.added:
            new_required = [f for f in self.added if f.is_required]
            if new_required:
                lines.append(f"  ⚠ {len(new_required)} new required field(s): "
                             + ", ".join(f.alias for f in new_required))
        if self.removed:
            lines.append(f"  ⚠ Removed fields: " + ", ".join(f.alias for f in self.removed))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "form_family": self.form_family,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "matched": [m.to_dict() for m in self.matched],
            "added": [f.to_dict() for f in self.added],
            "removed": [f.to_dict() for f in self.removed],
            "stats": {
                "matched_count": len(self.matched),
                "changed_count": self.changed_count,
                "safe_migration_count": self.safe_migration_count,
                "breaking_change_count": self.breaking_change_count,
                "added_count": len(self.added),
                "removed_count": len(self.removed),
                "new_required_count": sum(1 for f in self.added if f.is_required),
            },
        }


# ---------------------------------------------------------------------------
# Migration plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MigrationAction:
    """A single recommended action for migrating a prior fill payload."""

    kind: str               # "carry_forward" | "re_review" | "remove_key" | "add_required"
    old_alias: str | None   # The key in the prior payload
    new_alias: str | None   # The key to use in the new payload
    field_name: str | None
    description: str
    urgency: str            # "info" | "warning" | "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "old_alias": self.old_alias,
            "new_alias": self.new_alias,
            "field_name": self.field_name,
            "description": self.description,
            "urgency": self.urgency,
        }


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def diff_schemas(old: CanonicalSchema, new: CanonicalSchema) -> SchemaDiff:
    """Compare two versions of the same form family.

    Matching uses three strategies, applied in order:

    1. **Exact field_name match** — the PDF field name is the same.
    2. **Label fuzzy match** — normalised label strings are similar enough.
    3. **Position heuristic** — fields at similar page/bbox coordinates are
       tentatively paired when other strategies fail.
    """
    old_by_name: dict[str, CanonicalField] = {f.field_name: f for f in old.fields}
    new_by_name: dict[str, CanonicalField] = {f.field_name: f for f in new.fields}

    matched: list[FieldDiff] = []
    unmatched_old: list[CanonicalField] = []
    unmatched_new: list[CanonicalField] = list(new.fields)

    # ── Pass 1: exact field_name match ───────────────────────────────────
    matched_new_names: set[str] = set()
    for old_field in old.fields:
        if old_field.field_name in new_by_name:
            new_field = new_by_name[old_field.field_name]
            matched_new_names.add(old_field.field_name)
            diff = _field_diff(old_field, new_field, "exact_name", 1.0)
            matched.append(diff)
        else:
            unmatched_old.append(old_field)

    unmatched_new = [f for f in unmatched_new if f.field_name not in matched_new_names]

    # ── Pass 2: label fuzzy match on remaining ────────────────────────────
    still_unmatched_old: list[CanonicalField] = []
    still_unmatched_new = list(unmatched_new)

    for old_field in unmatched_old:
        best_new, score = _best_label_match(old_field, still_unmatched_new)
        if best_new is not None and score >= 0.75:
            still_unmatched_new.remove(best_new)
            diff = _field_diff(old_field, best_new, "label_fuzzy", score)
            matched.append(diff)
        else:
            still_unmatched_old.append(old_field)

    unmatched_old = still_unmatched_old
    unmatched_new = still_unmatched_new

    # ── Pass 3: position heuristic on remaining ───────────────────────────
    truly_removed: list[CanonicalField] = []
    truly_added = list(unmatched_new)

    for old_field in unmatched_old:
        best_new, score = _best_position_match(old_field, unmatched_new)
        if best_new is not None and score >= 0.80:
            truly_added.remove(best_new)
            diff = _field_diff(old_field, best_new, "position_heuristic", score)
            matched.append(diff)
        else:
            truly_removed.append(old_field)

    return SchemaDiff(
        old_version=old.version,
        new_version=new.version,
        form_family=old.form_family,
        matched=sorted(matched, key=lambda m: m.new_alias),
        added=truly_added,
        removed=truly_removed,
    )


def migration_plan(diff: SchemaDiff) -> list[MigrationAction]:
    """Produce an ordered list of actions to migrate a prior fill payload.

    The returned actions are ordered: carry-forward first (safe changes),
    then re-review items, then deletions, then new required fields.
    """
    actions: list[MigrationAction] = []

    for m in diff.matched:
        if not m.is_changed:
            actions.append(MigrationAction(
                kind="carry_forward",
                old_alias=m.old_alias,
                new_alias=m.new_alias,
                field_name=m.field_name,
                description=(
                    f"Field is unchanged. Rename key '{m.old_alias}' → '{m.new_alias}' "
                    "in your payload (if alias changed)."
                ) if m.old_alias != m.new_alias else (
                    f"Field '{m.old_alias}' is unchanged. Prior value is safe to reuse."
                ),
                urgency="info",
            ))
        elif m.is_safe_to_migrate:
            changed_attrs = [c.attribute for c in m.changes]
            actions.append(MigrationAction(
                kind="re_review",
                old_alias=m.old_alias,
                new_alias=m.new_alias,
                field_name=m.field_name,
                description=(
                    f"Field changed: {', '.join(changed_attrs)}. "
                    "Prior value type is still compatible — review the new label/context "
                    "and confirm the value is still correct."
                ),
                urgency="warning",
            ))
        else:
            changed_attrs = [c.attribute for c in m.changes]
            actions.append(MigrationAction(
                kind="re_review",
                old_alias=m.old_alias,
                new_alias=m.new_alias,
                field_name=m.field_name,
                description=(
                    f"Breaking change: {', '.join(changed_attrs)} changed. "
                    "Prior value may no longer be valid — provide a new value for this field."
                ),
                urgency="error",
            ))

    for removed in diff.removed:
        actions.append(MigrationAction(
            kind="remove_key",
            old_alias=removed.alias,
            new_alias=None,
            field_name=removed.field_name,
            description=(
                f"Field '{removed.alias}' ({removed.field_name}) was removed from the form. "
                "Remove this key from your fill payload."
            ),
            urgency="warning",
        ))

    for added in diff.added:
        actions.append(MigrationAction(
            kind="add_required" if added.is_required else "carry_forward",
            old_alias=None,
            new_alias=added.alias,
            field_name=added.field_name,
            description=(
                f"New {'required' if added.is_required else 'optional'} field "
                f"'{added.alias}' ({added.label or added.field_name}). "
                + ("Must be filled for the form to be accepted." if added.is_required
                   else "Fill if applicable.")
            ),
            urgency="error" if added.is_required else "info",
        ))

    return actions


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _field_diff(
    old: CanonicalField,
    new: CanonicalField,
    method: str,
    confidence: float,
) -> FieldDiff:
    changes: list[FieldChange] = []
    for attr in ("label", "field_type", "expected_value_type", "expected_format",
                 "is_required", "section", "context"):
        old_val = getattr(old, attr)
        new_val = getattr(new, attr)
        if old_val != new_val:
            changes.append(FieldChange(attribute=attr, old_value=old_val, new_value=new_val))
    return FieldDiff(
        old_alias=old.alias,
        new_alias=new.alias,
        field_name=new.field_name,
        match_method=method,
        match_confidence=round(confidence, 3),
        changes=changes,
    )


def _normalise_label(label: str | None) -> str:
    if not label:
        return ""
    import re
    return re.sub(r"[^a-z0-9]", "", label.lower())


def _label_similarity(a: str | None, b: str | None) -> float:
    na, nb = _normalise_label(a), _normalise_label(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # Dice coefficient on bigrams
    def bigrams(s: str) -> set[str]:
        return {s[i:i+2] for i in range(len(s) - 1)}
    ba, bb = bigrams(na), bigrams(nb)
    if not ba or not bb:
        return 0.5 if na in nb or nb in na else 0.0
    return 2 * len(ba & bb) / (len(ba) + len(bb))


def _best_label_match(
    target: CanonicalField,
    candidates: list[CanonicalField],
) -> tuple[CanonicalField | None, float]:
    best: CanonicalField | None = None
    best_score = 0.0
    for c in candidates:
        score = _label_similarity(target.label, c.label)
        # Bonus for same page
        if target.page == c.page:
            score = min(1.0, score + 0.05)
        if score > best_score:
            best_score = score
            best = c
    return best, best_score


def _bbox_proximity(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Return 0..1 where 1 means identical bbox, 0 means far apart."""
    ax = (a[0] + a[2]) / 2
    ay = (a[1] + a[3]) / 2
    bx = (b[0] + b[2]) / 2
    by = (b[1] + b[3]) / 2
    dist = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
    # Consider 0 distance → 1.0, 100 points apart → 0.0
    return max(0.0, 1.0 - dist / 100.0)


def _best_position_match(
    target: CanonicalField,
    candidates: list[CanonicalField],
) -> tuple[CanonicalField | None, float]:
    best: CanonicalField | None = None
    best_score = 0.0
    for c in candidates:
        if c.page != target.page:
            continue
        score = _bbox_proximity(target.bbox, c.bbox)
        if score > best_score:
            best_score = score
            best = c
    return best, best_score
