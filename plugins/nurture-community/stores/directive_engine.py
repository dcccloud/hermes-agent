"""DirectiveEngine (JSON backend) — strategy directives pushed to avatars.

Unlike Advice (reactive, rule-based), Directives are proactive strategy
instructions. Types: "weight" / "behavior" / "schedule".

PG backend: ``plugins/nurture-community/pg/pg_directive_engine.py``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence

from ._helpers import load_json, now_ms, save_json


DirectiveType = Literal["weight", "behavior", "schedule"]


@dataclass
class DirectiveInstruction:
    action: str
    target: str
    params: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DirectiveInstruction":
        return cls(
            action=data.get("action", ""),
            target=data.get("target", ""),
            params=dict(data.get("params") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DirectiveEntry:
    directiveId: str
    app: str
    type: DirectiveType
    summary: str
    instructions: List[DirectiveInstruction]
    priority: int
    createdAt: int
    deliveredTo: List[str] = field(default_factory=list)
    expiresAt: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DirectiveEntry":
        return cls(
            directiveId=data.get("directiveId", ""),
            app=data.get("app", ""),
            type=data.get("type", "behavior") or "behavior",
            summary=data.get("summary", ""),
            instructions=[
                DirectiveInstruction.from_dict(i)
                for i in (data.get("instructions") or [])
                if isinstance(i, dict)
            ],
            priority=int(data.get("priority", 5) or 5),
            createdAt=int(data.get("createdAt", 0) or 0),
            deliveredTo=list(data.get("deliveredTo") or []),
            expiresAt=data.get("expiresAt"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "directiveId": self.directiveId,
            "app": self.app,
            "type": self.type,
            "summary": self.summary,
            "instructions": [i.to_dict() for i in self.instructions],
            "priority": self.priority,
            "createdAt": self.createdAt,
            "deliveredTo": self.deliveredTo,
        }
        if self.expiresAt is not None:
            d["expiresAt"] = self.expiresAt
        return d


_directive_id_counter = count(1)


def _next_directive_id() -> str:
    return f"dir-{now_ms()}-{next(_directive_id_counter)}"


def _parse_iso(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


class DirectiveEngine:
    def __init__(self, store_dir: Path):
        self._store_file = Path(store_dir) / "directives.json"
        raw = load_json(self._store_file, [])
        self._directives: List[DirectiveEntry] = [
            DirectiveEntry.from_dict(d) for d in raw if isinstance(d, dict)
        ]

    @property
    def all(self) -> Sequence[DirectiveEntry]:
        return tuple(self._directives)

    @property
    def size(self) -> int:
        return len(self._directives)

    def create(
        self,
        *,
        app: str,
        type: DirectiveType,
        summary: str,
        instructions: List[DirectiveInstruction],
        priority: int = 5,
        expires_at: Optional[str] = None,
    ) -> DirectiveEntry:
        entry = DirectiveEntry(
            directiveId=_next_directive_id(),
            app=app,
            type=type,
            summary=summary,
            instructions=list(instructions),
            priority=priority,
            createdAt=now_ms(),
            deliveredTo=[],
            expiresAt=expires_at,
        )
        self._directives.append(entry)
        self._save()
        return entry

    def get(self, directive_id: str) -> Optional[DirectiveEntry]:
        return next(
            (d for d in self._directives if d.directiveId == directive_id), None
        )

    def get_for_app(self, app: str) -> List[DirectiveEntry]:
        now_s = now_ms() / 1000.0
        return [
            d for d in self._directives
            if d.app == app and (not d.expiresAt or _parse_iso(d.expiresAt) >= now_s)
        ]

    def get_pending_for_avatar(
        self, avatar_id: str, app: Optional[str] = None
    ) -> List[DirectiveEntry]:
        now_s = now_ms() / 1000.0
        results: List[DirectiveEntry] = []
        for d in self._directives:
            if avatar_id in d.deliveredTo:
                continue
            if app and d.app != app:
                continue
            if d.expiresAt and _parse_iso(d.expiresAt) < now_s:
                continue
            results.append(d)
        return results

    def mark_delivered(self, directive_id: str, avatar_id: str) -> None:
        entry = self.get(directive_id)
        if entry is not None and avatar_id not in entry.deliveredTo:
            entry.deliveredTo.append(avatar_id)
            self._save()

    def remove(self, directive_id: str) -> bool:
        for i, d in enumerate(self._directives):
            if d.directiveId == directive_id:
                self._directives.pop(i)
                self._save()
                return True
        return False

    def clean_expired(self) -> int:
        now_s = now_ms() / 1000.0
        before = len(self._directives)
        self._directives = [
            d for d in self._directives
            if not d.expiresAt or _parse_iso(d.expiresAt) >= now_s
        ]
        removed = before - len(self._directives)
        if removed:
            self._save()
        return removed

    def _save(self) -> None:
        save_json(self._store_file, [d.to_dict() for d in self._directives])
