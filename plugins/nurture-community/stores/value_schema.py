"""ValueSchema — manages the "valuable data fields" schema that tells agents
what data is worth observing during recipe execution.

The schema is versioned so agents can detect updates efficiently. When a
recipe is generated or optimized, the agent's recipe generator consults
this schema to add observed_data collection points.

JSON-only (matches OpenClaw — small dataset, version-bumped snapshot).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal

from ._helpers import load_json, now_ms, save_json


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

DataType = Literal["number", "string", "boolean"]
Priority = Literal["low", "normal", "high"]


@dataclass
class ValueField:
    name: str
    description: str
    observe_hint: str
    data_type: DataType
    priority: Priority


@dataclass
class ValueSchemaCategory:
    app: str
    category: str
    fields: List[ValueField] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ValueSchemaCategory":
        return cls(
            app=data.get("app", ""),
            category=data.get("category", ""),
            fields=[
                ValueField(**f) for f in data.get("fields", []) if isinstance(f, dict)
            ],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app": self.app,
            "category": self.category,
            "fields": [asdict(f) for f in self.fields],
        }


@dataclass
class ValueSchemaSnapshot:
    version: int = 0
    updatedAt: int = 0
    schemas: List[ValueSchemaCategory] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "updatedAt": self.updatedAt,
            "schemas": [s.to_dict() for s in self.schemas],
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ValueSchemaStore:
    """JSON-backed value schema store. ValueSchema is always JSON (data
    volume is tiny — version-bumped snapshot, see decisions.md ADR-002)."""

    def __init__(self, store_dir: Path):
        self._store_file = Path(store_dir) / "value-schema.json"
        raw = load_json(self._store_file, {"version": 0, "updatedAt": 0, "schemas": []})
        self._snapshot = ValueSchemaSnapshot(
            version=int(raw.get("version", 0) or 0),
            updatedAt=int(raw.get("updatedAt", 0) or 0),
            schemas=[
                ValueSchemaCategory.from_dict(s)
                for s in raw.get("schemas", [])
                if isinstance(s, dict)
            ],
        )

    @property
    def version(self) -> int:
        return self._snapshot.version

    def get_snapshot(self) -> ValueSchemaSnapshot:
        return self._snapshot

    def get_for_app(self, app: str) -> List[ValueSchemaCategory]:
        return [s for s in self._snapshot.schemas if s.app == app]

    def get_apps(self) -> List[str]:
        return list(dict.fromkeys(s.app for s in self._snapshot.schemas))

    def upsert_category(self, category: ValueSchemaCategory) -> None:
        """Insert or replace a category; bumps version."""
        for i, s in enumerate(self._snapshot.schemas):
            if s.app == category.app and s.category == category.category:
                self._snapshot.schemas[i] = category
                break
        else:
            self._snapshot.schemas.append(category)
        self._bump_and_save()

    def remove_category(self, app: str, category: str) -> bool:
        """Remove a category; returns True if found."""
        for i, s in enumerate(self._snapshot.schemas):
            if s.app == app and s.category == category:
                self._snapshot.schemas.pop(i)
                self._bump_and_save()
                return True
        return False

    def replace_all(self, schemas: List[ValueSchemaCategory]) -> None:
        """Bulk import — replaces entire schema list."""
        self._snapshot.schemas = list(schemas)
        self._bump_and_save()

    def build_sync_payload(self) -> Dict[str, Any]:
        """Payload for ``nurture.valueschema.sync``."""
        return {
            "version": self._snapshot.version,
            "schemas": [s.to_dict() for s in self._snapshot.schemas],
        }

    # -- internal -----------------------------------------------------------

    def _bump_and_save(self) -> None:
        self._snapshot.version += 1
        self._snapshot.updatedAt = now_ms()
        save_json(self._store_file, self._snapshot.to_dict())
