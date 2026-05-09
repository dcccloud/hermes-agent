"""PG-backed TraceStore. Mirrors stores/trace_store.py JSON API."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..stores._helpers import now_ms
from ..stores.trace_store import TraceAction, TraceEntry


class PgTraceStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def ingest(self, entry: Dict[str, Any]) -> TraceEntry:
        full = TraceEntry.from_dict({**entry, "ingestedAt": now_ms()})
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO nurture_traces
                  (trace_id, agent_id, device_id, app, operation, step,
                   timestamp, actions, success, duration_ms, task_id, extra)
                VALUES ($1,$2,$3,$4,$5,$6,$7::timestamptz,$8::jsonb,$9,$10,$11,$12::jsonb)
                ON CONFLICT (trace_id) DO NOTHING
                """,
                full.traceId, full.agentId, full.deviceId, full.app, full.operation,
                full.step, full.timestamp,
                json.dumps([a.to_dict() for a in full.actions]),
                full.success, full.duration_ms, full.taskId,
                json.dumps(full.extra) if full.extra is not None else None,
            )
        return full

    async def size(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS c FROM nurture_traces")
        return int(row["c"]) if row else 0

    async def get_by_app(self, app: str) -> List[TraceEntry]:
        return await self._fetch("WHERE app = $1 ORDER BY timestamp DESC", app)

    async def get_by_operation(self, app: str, operation: str) -> List[TraceEntry]:
        return await self._fetch(
            "WHERE app = $1 AND operation = $2 ORDER BY timestamp DESC", app, operation,
        )

    async def get_by_agent(self, agent_id: str) -> List[TraceEntry]:
        return await self._fetch(
            "WHERE agent_id = $1 ORDER BY timestamp DESC", agent_id
        )

    async def get_by_task_id(self, task_id: str) -> List[TraceEntry]:
        return await self._fetch(
            "WHERE task_id = $1 ORDER BY timestamp", task_id
        )

    async def get_by_time_range(
        self, since: str, until: Optional[str] = None
    ) -> List[TraceEntry]:
        if until:
            return await self._fetch(
                "WHERE timestamp >= $1::timestamptz AND timestamp <= $2::timestamptz "
                "ORDER BY timestamp",
                since, until,
            )
        return await self._fetch(
            "WHERE timestamp >= $1::timestamptz ORDER BY timestamp", since
        )

    async def get_recent(self, limit: int = 50) -> List[TraceEntry]:
        return await self._fetch("ORDER BY ingested_at DESC LIMIT $1", limit)

    async def extract_observed_data(
        self,
        app: str,
        *,
        since: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """SQL: pull traces, extract observed_data fields client-side
        (mirrors JSON-store implementation)."""
        traces = await self.get_by_app(app)
        if since:
            from ..stores.trace_store import _parse_iso  # type: ignore
            since_ts = _parse_iso(since)
            traces = [t for t in traces if _parse_iso(t.timestamp) >= since_ts]
        if agent_id:
            traces = [t for t in traces if t.agentId == agent_id]
        results: List[Dict[str, Any]] = []
        for t in traces:
            for action in t.actions:
                if action.observed_data:
                    results.append({
                        "traceId": t.traceId, "agentId": t.agentId,
                        "timestamp": t.timestamp, "data": action.observed_data,
                    })
        return results

    async def get_success_rate(self, app: str, operation: str) -> Dict[str, float]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  COUNT(*) FILTER (WHERE success) AS s,
                  COUNT(*) AS t
                FROM nurture_traces WHERE app = $1 AND operation = $2
                """,
                app, operation,
            )
        total = int(row["t"]) if row else 0
        successes = int(row["s"]) if row else 0
        return {
            "rate": (successes / total) if total else 0.0,
            "total": total,
        }

    async def _fetch(self, where_clause: str, *args: Any) -> List[TraceEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM nurture_traces {where_clause}", *args
            )
        return [_row_to_trace(r) for r in rows]


def _row_to_trace(row: Any) -> TraceEntry:
    actions_raw = row["actions"]
    if isinstance(actions_raw, str):
        actions_raw = json.loads(actions_raw)
    extra = row["extra"]
    if isinstance(extra, str):
        extra = json.loads(extra)
    ts = row["timestamp"]
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    ingested = row["ingested_at"]
    if isinstance(ingested, datetime):
        ingested = int(ingested.timestamp() * 1000)
    return TraceEntry(
        traceId=row["trace_id"],
        agentId=row["agent_id"],
        deviceId=row["device_id"] or "",
        app=row["app"],
        operation=row["operation"],
        step=row["step"] or "",
        timestamp=ts or "",
        actions=[TraceAction.from_dict(a) for a in (actions_raw or [])],
        success=bool(row["success"]),
        ingestedAt=int(ingested or 0),
        duration_ms=row["duration_ms"],
        taskId=row["task_id"],
        extra=extra,
    )
