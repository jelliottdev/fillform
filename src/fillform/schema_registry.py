"""Schema registry with in-memory cache and optional disk persistence.

Schemas are keyed by (form_family, version).  When *storage_dir* is set, every
``put()`` writes a JSON file and ``get()`` / ``latest_for_family()`` fall back to
disk if the schema is not in memory.  This lets schemas survive MCP process
restarts and be shared across sessions.

File naming convention::

    <storage_dir>/<form_family>_schema_v<version>.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import CanonicalSchema


@dataclass(slots=True)
class SchemaRegistry:
    """Two-layer schema store: fast in-memory cache backed by optional JSON files."""

    _schemas: dict[str, dict[str, CanonicalSchema]] = field(default_factory=dict)
    storage_dir: Path | None = None

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def put(self, schema: CanonicalSchema) -> None:
        """Store *schema* in memory and, if a storage directory is configured, on disk."""
        self._schemas.setdefault(schema.form_family, {})[schema.version] = schema
        if self.storage_dir is not None:
            self._save_to_disk(schema)

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get(self, form_family: str, version: str) -> CanonicalSchema | None:
        """Return the schema for *form_family* / *version*, checking disk if needed."""
        cached = self._schemas.get(form_family, {}).get(version)
        if cached is not None:
            return cached
        if self.storage_dir is not None:
            return self._load_from_disk(form_family, version)
        return None

    def latest_for_family(self, form_family: str) -> CanonicalSchema | None:
        """Return the lexicographically latest version for *form_family*."""
        versions = self._schemas.get(form_family)
        if versions:
            return versions[sorted(versions)[-1]]
        if self.storage_dir is not None:
            return self._load_latest_from_disk(form_family)
        return None

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    def list_families(self) -> list[str]:
        """Return all known form families (in-memory + on-disk)."""
        families: set[str] = set(self._schemas.keys())
        if self.storage_dir is not None and self.storage_dir.exists():
            for path in self.storage_dir.glob("*_schema_v*.json"):
                stem = path.stem  # e.g. "w9_schema_v2"
                if "_schema_v" in stem:
                    family_part = stem.rsplit("_schema_v", 1)[0]
                    families.add(family_part)
        return sorted(families)

    def list_versions(self, form_family: str) -> list[str]:
        """Return all known versions for *form_family* (in-memory + on-disk)."""
        versions: set[str] = set((self._schemas.get(form_family) or {}).keys())
        if self.storage_dir is not None and self.storage_dir.exists():
            pattern = f"{form_family}_schema_v*.json"
            for path in self.storage_dir.glob(pattern):
                stem = path.stem
                if "_schema_v" in stem:
                    ver = stem.rsplit("_schema_v", 1)[1]
                    versions.add(ver)
        return sorted(versions)

    # ------------------------------------------------------------------
    # Disk helpers
    # ------------------------------------------------------------------

    def _disk_path(self, form_family: str, version: str) -> Path:
        assert self.storage_dir is not None
        return self.storage_dir / f"{form_family}_schema_v{version}.json"

    def _save_to_disk(self, schema: CanonicalSchema) -> None:
        assert self.storage_dir is not None
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        path = self._disk_path(schema.form_family, schema.version)
        path.write_text(json.dumps(schema.to_dict(), indent=2), encoding="utf-8")

    def _load_from_disk(self, form_family: str, version: str) -> CanonicalSchema | None:
        assert self.storage_dir is not None
        path = self._disk_path(form_family, version)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            schema = CanonicalSchema.from_dict(data)
            # Warm the in-memory cache
            self._schemas.setdefault(form_family, {})[version] = schema
            return schema
        except Exception:
            return None

    def _load_latest_from_disk(self, form_family: str) -> CanonicalSchema | None:
        assert self.storage_dir is not None
        if not self.storage_dir.exists():
            return None
        candidates = sorted(self.storage_dir.glob(f"{form_family}_schema_v*.json"))
        if not candidates:
            return None
        path = candidates[-1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            schema = CanonicalSchema.from_dict(data)
            self._schemas.setdefault(form_family, {})[schema.version] = schema
            return schema
        except Exception:
            return None
