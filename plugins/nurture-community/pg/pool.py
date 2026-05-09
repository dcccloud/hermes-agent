"""PostgreSQL connection pool + migration runner.

Mirrors OpenClaw's ``pg-pool.ts`` parameters (max=10, idle=30s, connect=5s).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

try:
    import asyncpg
except ImportError:  # pragma: no cover — only when pip dep missing
    asyncpg = None  # type: ignore[assignment]


_MIGRATION_PATH = Path(__file__).parent / "migration.sql"


async def create_pool(database_url: str) -> "asyncpg.Pool":
    """Create an asyncpg connection pool.

    Pool parameters match OpenClaw's node-pg config:
      - min_size=2 (warm)
      - max_size=10
      - command_timeout=30s
      - timeout=5s for connection acquisition
    """
    if asyncpg is None:
        raise RuntimeError(
            "asyncpg is not installed. Add `asyncpg` to your venv to enable PG backend."
        )
    return await asyncpg.create_pool(
        dsn=database_url,
        min_size=2,
        max_size=10,
        command_timeout=30.0,
        timeout=5.0,
    )


async def run_migration(pool: "asyncpg.Pool") -> None:
    """Execute migration.sql against the pool. Idempotent (all CREATE
    statements use IF NOT EXISTS)."""
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def close_pool(pool: Optional["asyncpg.Pool"]) -> None:
    if pool is None:
        return
    await pool.close()
