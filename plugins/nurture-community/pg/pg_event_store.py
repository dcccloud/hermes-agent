"""PG-backed EventStore. Mirrors stores/event_store.py JSON API."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..stores._helpers import now_ms
from ..stores.event_store import EventEntry


class PgEventStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def ingest(self, entry: Dict[str, Any]) -> EventEntry:
        """Ingest event. Deduplicates by event_id (ON CONFLICT DO NOTHING)."""
        full = EventEntry.from_dict({**entry, "ingestedAt": now_ms()})
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO nurture_events
                  (event_id, agent_id, device_id, event_type, timestamp,
                   operation, step, task_id, payload)
                VALUES ($1, $2, $3, $4, $5::timestamptz, $6, $7, $8, $9::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                """,
                full.eventId, full.agentId, full.deviceId, full.eventType,
                full.timestamp, full.operation, full.step, full.taskId,
                json.dumps(full.payload),
            )
        return full

    async def size(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS c FROM nurture_events")
        return int(row["c"]) if row else 0

    async def get_by_agent(self, agent_id: str) -> List[EventEntry]:
        return await self._fetch(
            "WHERE agent_id = $1 ORDER BY timestamp DESC", agent_id
        )

    async def get_by_event_type(self, event_type: str) -> List[EventEntry]:
        return await self._fetch(
            "WHERE event_type = $1 ORDER BY timestamp DESC", event_type
        )

    async def get_by_operation(self, operation: str) -> List[EventEntry]:
        return await self._fetch(
            "WHERE operation = $1 ORDER BY timestamp DESC", operation
        )

    async def get_by_time_range(
        self, since: str, until: Optional[str] = None
    ) -> List[EventEntry]:
        if until:
            return await self._fetch(
                "WHERE timestamp >= $1::timestamptz AND timestamp <= $2::timestamptz "
                "ORDER BY timestamp",
                since, until,
            )
        return await self._fetch(
            "WHERE timestamp >= $1::timestamptz ORDER BY timestamp", since
        )

    async def get_recent(self, limit: int = 50) -> List[EventEntry]:
        return await self._fetch(
            "ORDER BY ingested_at DESC LIMIT $1", limit
        )

    async def _fetch(self, where_clause: str, *args: Any) -> List[EventEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM nurture_events {where_clause}", *args
            )
        return [_row_to_entry(r) for r in rows]


def _row_to_entry(row: Any) -> EventEntry:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    ts = row["timestamp"]
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    ingested = row["ingested_at"]
    if isinstance(ingested, datetime):
        ingested = int(ingested.timestamp() * 1000)
    return EventEntry(
        eventId=row["event_id"],
        agentId=row["agent_id"],
        deviceId=row["device_id"] or "",
        eventType=row["event_type"],
        timestamp=ts or "",
        operation=row["operation"] or "",
        step=row["step"] or "",
        payload=payload or {},
        ingestedAt=int(ingested or 0),
        taskId=row["task_id"],
    )
