"""In-memory schema registry keyed by form family and version."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import CanonicalSchema


@dataclass(slots=True)
class SchemaRegistry:
    _schemas: dict[str, dict[str, CanonicalSchema]] = field(default_factory=dict)

    def put(self, schema: CanonicalSchema) -> None:
        self._schemas.setdefault(schema.form_family, {})[schema.version] = schema

    def get(self, form_family: str, version: str) -> CanonicalSchema | None:
        return self._schemas.get(form_family, {}).get(version)

    def latest_for_family(self, form_family: str) -> CanonicalSchema | None:
        versions = self._schemas.get(form_family)
        if not versions:
            return None
        return versions[sorted(versions)[-1]]
