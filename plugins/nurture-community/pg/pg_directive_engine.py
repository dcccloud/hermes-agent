"""PG-backed DirectiveEngine."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional

from ..stores._helpers import now_ms
from ..stores.directive_engine import (
    DirectiveEntry,
    DirectiveInstruction,
    DirectiveType,
    _next_directive_id,
)


class PgDirectiveEngine:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def create(
        self, *, app: str, type: DirectiveType, summary: str,
        instructions: List[DirectiveInstruction],
        priority: int = 5, expires_at: Optional[str] = None,
    ) -> DirectiveEntry:
        directive_id = _next_directive_id()
        created_at = now_ms()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO nurture_directives
                  (directive_id, app, type, summary, instructions,
                   priority, created_at, expires_at, delivered_to)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8::timestamptz,'[]'::jsonb)
                """,
                directive_id, app, type, summary,
                json.dumps([i.to_dict() for i in instructions]),
                priority, created_at, expires_at,
            )
        return DirectiveEntry(
            directiveId=directive_id, app=app, type=type, summary=summary,
            instructions=list(instructions), priority=priority,
            createdAt=created_at, deliveredTo=[], expiresAt=expires_at,
        )

    async def get(self, directive_id: str) -> Optional[DirectiveEntry]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM nurture_directives WHERE directive_id = $1",
                directive_id,
            )
        return _row_to_entry(row) if row else None

    async def get_for_app(self, app: str) -> List[DirectiveEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM nurture_directives
                WHERE app = $1 AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY priority DESC, created_at DESC
                """,
                app,
            )
        return [_row_to_entry(r) for r in rows]

    async def get_pending_for_avatar(
        self, avatar_id: str, app: Optional[str] = None
    ) -> List[DirectiveEntry]:
        clauses = ["NOT (delivered_to @> $1::jsonb)",
                   "(expires_at IS NULL OR expires_at > NOW())"]
        args: List[Any] = [json.dumps([avatar_id])]
        if app:
            args.append(app)
            clauses.append(f"app = ${len(args)}")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM nurture_directives WHERE {' AND '.join(clauses)} "
                f"ORDER BY priority DESC, created_at DESC",
                *args,
            )
        return [_row_to_entry(r) for r in rows]

    async def mark_delivered(self, directive_id: str, avatar_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE nurture_directives
                SET delivered_to = delivered_to || $2::jsonb
                WHERE directive_id = $1
                  AND NOT (delivered_to @> $2::jsonb)
                """,
                directive_id, json.dumps([avatar_id]),
            )

    async def remove(self, directive_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM nurture_directives WHERE directive_id = $1",
                directive_id,
            )
        parts = result.split()
        return parts and parts[-1].isdigit() and int(parts[-1]) > 0

    async def clean_expired(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM nurture_directives
                WHERE expires_at IS NOT NULL AND expires_at < NOW()
                """
            )
        parts = result.split()
        return int(parts[-1]) if parts and parts[-1].isdigit() else 0


def _row_to_entry(row: Any) -> DirectiveEntry:
    def _decode(v: Any) -> Any:
        return json.loads(v) if isinstance(v, str) else v
    instructions_raw = _decode(row["instructions"]) or []
    expires_at = row["expires_at"]
    if isinstance(expires_at, datetime):
        expires_at = expires_at.isoformat().replace("+00:00", "Z")
    return DirectiveEntry(
        directiveId=row["directive_id"], app=row["app"], type=row["type"],
        summary=row["summary"],
        instructions=[DirectiveInstruction.from_dict(i) for i in instructions_raw],
        priority=int(row["priority"] or 5),
        createdAt=int(row["created_at"]),
        deliveredTo=list(_decode(row["delivered_to"]) or []),
        expiresAt=expires_at,
    )
