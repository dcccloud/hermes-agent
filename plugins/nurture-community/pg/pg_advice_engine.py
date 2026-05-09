"""PG-backed AdviceEngine."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..stores._helpers import now_ms
from ..stores.advice_engine import (
    BUILTIN_RULES,
    AdviceEntry,
    AdviceRule,
    AdviceRuleDefinition,
    TraceStats,
    _next_advice_id,
)


class PgAdviceEngine:
    def __init__(
        self, pool: Any,
        extra_rules: Optional[List[AdviceRuleDefinition]] = None,
    ) -> None:
        self._pool = pool
        self._rules: List[AdviceRuleDefinition] = [
            *BUILTIN_RULES, *(extra_rules or [])
        ]

    @property
    async def size(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS c FROM nurture_advices")
        return int(row["c"]) if row else 0

    async def analyze(self, trace_store: Any, app: str) -> int:
        stats = await self._build_stats(trace_store, app)
        generated = 0
        now = now_ms()
        async with self._pool.acquire() as conn:
            for rule in self._rules:
                if rule.app != "*" and rule.app != app:
                    continue
                candidate = rule.evaluate(stats)
                if candidate is None:
                    continue
                duplicate = await conn.fetchrow(
                    """
                    SELECT 1 FROM nurture_advices
                    WHERE app=$1 AND type=$2 AND summary=$3
                      AND created_at >= $4
                    LIMIT 1
                    """,
                    app, candidate.type, candidate.summary,
                    now - 24 * 60 * 60 * 1000,
                )
                if duplicate:
                    continue
                await conn.execute(
                    """
                    INSERT INTO nurture_advices
                      (advice_id, app, type, summary, rules,
                       confidence, based_on_traces, created_at, delivered_to)
                    VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,'[]'::jsonb)
                    """,
                    _next_advice_id(), app, candidate.type, candidate.summary,
                    json.dumps([r.to_dict() for r in candidate.rules]),
                    candidate.confidence, stats.totalTraces, now,
                )
                generated += 1
        return generated

    async def get_pending_for_agent(
        self, agent_id: str, app: Optional[str] = None
    ) -> List[AdviceEntry]:
        clauses = ["NOT (delivered_to @> $1::jsonb)",
                   "(expires_at IS NULL OR expires_at > NOW())"]
        args: List[Any] = [json.dumps([agent_id])]
        if app:
            args.append(app)
            clauses.append(f"app = ${len(args)}")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM nurture_advices WHERE {' AND '.join(clauses)} "
                f"ORDER BY created_at DESC",
                *args,
            )
        return [_row_to_entry(r) for r in rows]

    async def mark_delivered(self, advice_id: str, agent_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE nurture_advices
                SET delivered_to = delivered_to || $2::jsonb
                WHERE advice_id = $1
                  AND NOT (delivered_to @> $2::jsonb)
                """,
                advice_id, json.dumps([agent_id]),
            )

    async def get_for_app(self, app: str) -> List[AdviceEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM nurture_advices WHERE app = $1 ORDER BY created_at",
                app,
            )
        return [_row_to_entry(r) for r in rows]

    async def _build_stats(self, trace_store: Any, app: str) -> TraceStats:
        # Reuse JSON-backend stats logic by fetching traces via the
        # async PG trace store.
        traces = await trace_store.get_by_app(app)

        op_groups: Dict[str, Dict[str, int]] = {}
        for t in traces:
            g = op_groups.setdefault(t.operation, {"success": 0, "total": 0})
            g["total"] += 1
            if t.success:
                g["success"] += 1
        operation_rates = {
            op: {
                "rate": (g["success"] / g["total"]) if g["total"] else 0.0,
                "total": g["total"],
            }
            for op, g in op_groups.items()
        }

        traces_per_day: Dict[str, int] = {}
        for t in traces:
            day = (t.timestamp or "")[:10]
            if day:
                traces_per_day[day] = traces_per_day.get(day, 0) + 1

        observed_map: Dict[str, List[Any]] = {}
        observed = await trace_store.extract_observed_data(app)
        for o in observed:
            for field_name, value in (o.get("data") or {}).items():
                observed_map.setdefault(field_name, []).append(value)
        observed_data = [
            {"field": name, "values": values}
            for name, values in observed_map.items()
        ]

        total = len(traces)
        success_count = sum(1 for t in traces if t.success)
        return TraceStats(
            app=app, totalTraces=total,
            successRate=(success_count / total) if total else 0.0,
            observedData=observed_data,
            operationRates=operation_rates,
            tracesPerDay=traces_per_day,
        )


def _row_to_entry(row: Any) -> AdviceEntry:
    def _decode(v: Any) -> Any:
        return json.loads(v) if isinstance(v, str) else v
    expires_at = row["expires_at"]
    if isinstance(expires_at, datetime):
        expires_at = expires_at.isoformat().replace("+00:00", "Z")
    return AdviceEntry(
        adviceId=row["advice_id"], app=row["app"], type=row["type"],
        summary=row["summary"],
        rules=[AdviceRule.from_dict(r) for r in (_decode(row["rules"]) or [])],
        confidence=float(row["confidence"] or 0),
        basedOnTraces=int(row["based_on_traces"] or 0),
        createdAt=int(row["created_at"]),
        deliveredTo=list(_decode(row["delivered_to"]) or []),
        expiresAt=expires_at,
    )
