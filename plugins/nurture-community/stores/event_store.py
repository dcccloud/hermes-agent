"""EventStore (JSON backend) — community-level storage for agent
operational events.

Receives lightweight timeline events reported by agents and provides
query APIs. Circular buffer with default 20k capacity.

PG backend: see ``plugins/nurture-community/pg/pg_event_store.py``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ._helpers import load_json, now_ms, save_json


@dataclass
class EventEntry:
    eventId: str
    agentId: str
    deviceId: str
    eventType: str
    timestamp: str
    operation: str
    step: str
    payload: Dict[str, Any]
    ingestedAt: int
    taskId: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventEntry":
        return cls(
            eventId=data.get("eventId", ""),
            agentId=data.get("agentId", ""),
            deviceId=data.get("deviceId", ""),
            eventType=data.get("eventType", ""),
            timestamp=data.get("timestamp", ""),
            operation=data.get("operation", ""),
            step=data.get("step", ""),
            payload=data.get("payload", {}) or {},
            ingestedAt=int(data.get("ingestedAt", 0) or 0),
            taskId=data.get("taskId"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d.get("taskId") is None:
            d.pop("taskId", None)
        return d


class EventStore:
    """JSON-backed event store. Circular buffer with default 20k capacity."""

    def __init__(self, store_dir: Path, *, max_events: int = 20_000):
        self._store_file = Path(store_dir) / "events.json"
        self._max_events = max_events
        raw = load_json(self._store_file, [])
        self._events: List[EventEntry] = [
            EventEntry.from_dict(e) for e in raw if isinstance(e, dict)
        ]

    @property
    def all(self) -> Sequence[EventEntry]:
        return tuple(self._events)

    @property
    def size(self) -> int:
        return len(self._events)

    def ingest(self, entry: Dict[str, Any]) -> EventEntry:
        """Ingest an event. Deduplicates by ``eventId``; evicts oldest on overflow."""
        event_id = entry.get("eventId", "")
        for existing in self._events:
            if existing.eventId == event_id:
                return existing

        full = EventEntry.from_dict({**entry, "ingestedAt": now_ms()})
        self._events.append(full)

        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]

        self._save()
        return full

    # -- queries ------------------------------------------------------------

    def get_by_agent(self, agent_id: str) -> List[EventEntry]:
        return [e for e in self._events if e.agentId == agent_id]

    def get_by_event_type(self, event_type: str) -> List[EventEntry]:
        return [e for e in self._events if e.eventType == event_type]

    def get_by_operation(self, operation: str) -> List[EventEntry]:
        return [e for e in self._events if e.operation == operation]

    def get_by_time_range(self, since: str, until: Optional[str] = None) -> List[EventEntry]:
        from datetime import datetime

        def _parse(s: str) -> float:
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                return 0.0

        since_ts = _parse(since)
        until_ts = _parse(until) if until else float("inf")
        return [
            e
            for e in self._events
            if since_ts <= _parse(e.timestamp) <= until_ts
        ]

    def get_recent(self, limit: int = 50) -> List[EventEntry]:
        return list(reversed(self._events[-limit:]))

    # -- internal -----------------------------------------------------------

    def _save(self) -> None:
        save_json(self._store_file, [e.to_dict() for e in self._events])
