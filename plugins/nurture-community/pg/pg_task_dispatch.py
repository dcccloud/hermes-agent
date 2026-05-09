"""PG-backed TaskDispatchEngine."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Any, Dict, List, Optional

from ..stores._helpers import now_ms
from ..stores.task_dispatch import (
    Priority,
    TaskDispatch,
    TaskRequirements,
    TaskStatus,
)


_pg_task_id_counter = count(1)


def _next_task_id() -> str:
    return f"task-{now_ms()}-{next(_pg_task_id_counter)}"


class PgTaskDispatchEngine:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def create(
        self,
        *,
        requirements: TaskRequirements,
        app: str,
        description: str,
        priority: Priority = "normal",
        expires_at: Optional[str] = None,
    ) -> TaskDispatch:
        if not expires_at:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat().replace("+00:00", "Z")
        task_id = _next_task_id()
        created_at = now_ms()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO nurture_tasks
                  (task_id, requirements, app, description, priority,
                   created_at, expires_at, status)
                VALUES ($1,$2::jsonb,$3,$4,$5,$6,$7::timestamptz,'active')
                """,
                task_id, json.dumps(requirements.to_dict()), app, description,
                priority, created_at, expires_at,
            )
        return TaskDispatch(
            taskId=task_id, requirements=requirements, app=app,
            description=description, priority=priority,
            createdAt=created_at, expiresAt=expires_at, status="active",
        )

    async def get(self, task_id: str) -> Optional[TaskDispatch]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM nurture_tasks WHERE task_id = $1", task_id
            )
        return _row_to_task(row) if row else None

    async def get_available(
        self, *, platform: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
    ) -> List[TaskDispatch]:
        # Application-layer filter: requirements is JSONB and platform/cap
        # filters depend on the avatar — easiest to filter in Python.
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM nurture_tasks
                WHERE status = 'active' AND expires_at > NOW()
                """
            )
        results: List[TaskDispatch] = []
        for r in rows:
            t = _row_to_task(r)
            req = t.requirements
            if platform and req.platform and platform not in req.platform:
                continue
            if capabilities and req.minCapabilities:
                if not all(cap in capabilities for cap in req.minCapabilities):
                    continue
            results.append(t)
        return results

    async def complete(self, task_id: str) -> Optional[TaskDispatch]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE nurture_tasks
                SET status = 'completed', completed_at = $2
                WHERE task_id = $1 AND status = 'active'
                RETURNING *
                """,
                task_id, now_ms(),
            )
        return _row_to_task(row) if row else None

    async def query(
        self, *, status: Optional[TaskStatus] = None,
        app: Optional[str] = None,
    ) -> List[TaskDispatch]:
        clauses: List[str] = []
        args: List[Any] = []
        if status:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        if app:
            args.append(app)
            clauses.append(f"app = ${len(args)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM nurture_tasks {where} ORDER BY created_at DESC",
                *args,
            )
        return [_row_to_task(r) for r in rows]

    async def clean_expired(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE nurture_tasks SET status = 'expired'
                WHERE status = 'active' AND expires_at < NOW()
                """
            )
        return _parse_pg_count(result)

    async def prune(self, max_age_ms: int = 7 * 24 * 3_600_000) -> int:
        cutoff = now_ms() - max_age_ms
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM nurture_tasks
                WHERE status <> 'active' AND created_at < $1
                """,
                cutoff,
            )
        return _parse_pg_count(result)


def _row_to_task(row: Any) -> TaskDispatch:
    req = row["requirements"]
    if isinstance(req, str):
        req = json.loads(req)
    expires_at = row["expires_at"]
    if isinstance(expires_at, datetime):
        expires_at = expires_at.isoformat().replace("+00:00", "Z")
    return TaskDispatch(
        taskId=row["task_id"],
        requirements=TaskRequirements.from_dict(req or {}),
        app=row["app"], description=row["description"],
        priority=row["priority"] or "normal",
        createdAt=int(row["created_at"]),
        expiresAt=expires_at or "",
        status=row["status"] or "active",
        completedAt=row.get("completed_at") if hasattr(row, "get") else row["completed_at"],
    )


def _parse_pg_count(execute_result: str) -> int:
    """``conn.execute`` returns strings like 'UPDATE 3' or 'DELETE 0'."""
    parts = execute_result.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0
