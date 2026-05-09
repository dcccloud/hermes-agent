"""AdviceEngine (JSON backend) — generates strategy advice from
aggregated trace data.

Initial version: rule-based analysis. Future versions can integrate
LLM analysis for nuanced insights.

PG backend: ``plugins/nurture-community/pg/pg_advice_engine.py``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence

from ._helpers import load_json, now_ms, save_json


AdviceType = Literal["strategy", "frequency", "timing", "content", "risk"]


@dataclass
class AdviceRule:
    condition: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AdviceRule":
        return cls(
            condition=data.get("condition", ""),
            action=data.get("action", ""),
            params=dict(data.get("params") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"condition": self.condition, "action": self.action}
        if self.params:
            d["params"] = self.params
        return d


@dataclass
class AdviceEntry:
    adviceId: str
    app: str
    type: AdviceType
    summary: str
    rules: List[AdviceRule]
    confidence: float
    basedOnTraces: int
    createdAt: int
    deliveredTo: List[str] = field(default_factory=list)
    expiresAt: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AdviceEntry":
        return cls(
            adviceId=data.get("adviceId", ""),
            app=data.get("app", ""),
            type=data.get("type", "strategy") or "strategy",
            summary=data.get("summary", ""),
            rules=[
                AdviceRule.from_dict(r)
                for r in (data.get("rules") or [])
                if isinstance(r, dict)
            ],
            confidence=float(data.get("confidence", 0) or 0),
            basedOnTraces=int(data.get("basedOnTraces", 0) or 0),
            createdAt=int(data.get("createdAt", 0) or 0),
            deliveredTo=list(data.get("deliveredTo") or []),
            expiresAt=data.get("expiresAt"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "adviceId": self.adviceId,
            "app": self.app,
            "type": self.type,
            "summary": self.summary,
            "rules": [r.to_dict() for r in self.rules],
            "confidence": self.confidence,
            "basedOnTraces": self.basedOnTraces,
            "createdAt": self.createdAt,
            "deliveredTo": self.deliveredTo,
        }
        if self.expiresAt is not None:
            d["expiresAt"] = self.expiresAt
        return d


@dataclass
class TraceStats:
    """Aggregated trace stats fed to AdviceRuleDefinition.evaluate()."""
    app: str
    totalTraces: int
    successRate: float
    observedData: List[Dict[str, Any]]   # [{"field": str, "values": [...]}]
    operationRates: Dict[str, Dict[str, float]]  # op -> {"rate": float, "total": int}
    tracesPerDay: Dict[str, int]


@dataclass
class AdviceCandidate:
    type: AdviceType
    summary: str
    rules: List[AdviceRule]
    confidence: float


@dataclass
class AdviceRuleDefinition:
    id: str
    app: str
    type: AdviceType
    description: str
    evaluate: Callable[[TraceStats], Optional[AdviceCandidate]]


_advice_id_counter = count(1)


def _next_advice_id() -> str:
    return f"advice-{now_ms()}-{next(_advice_id_counter)}"


# ---------------------------------------------------------------------------
# Built-in rules
# ---------------------------------------------------------------------------


def _rule_high_failure_rate(stats: TraceStats) -> Optional[AdviceCandidate]:
    failing = [
        (op, r) for op, r in stats.operationRates.items()
        if r["total"] >= 5 and r["rate"] < 0.5
    ]
    if not failing:
        return None
    ops = ", ".join(f"{op} ({int(r['rate'] * 100)}%)" for op, r in failing)
    return AdviceCandidate(
        type="risk",
        summary=(
            f"High failure rate detected: {ops}. "
            f"Consider reviewing or regenerating recipes."
        ),
        rules=[
            AdviceRule(
                condition=f'operation_success_rate("{op}") < 0.5',
                action="review_recipe",
                params={"operation": op},
            )
            for op, _ in failing
        ],
        confidence=0.8,
    )


def _rule_high_frequency(stats: TraceStats) -> Optional[AdviceCandidate]:
    days = list(stats.tracesPerDay.items())
    high_days = [(day, c) for day, c in days if c > 50]
    if not high_days:
        return None
    avg = sum(c for _, c in days) / max(len(days), 1)
    return AdviceCandidate(
        type="frequency",
        summary=(
            f"High activity detected: {len(high_days)} day(s) with >50 operations "
            f"(avg: {int(avg)}/day). Consider reducing frequency."
        ),
        rules=[
            AdviceRule(
                condition="daily_operations > 50",
                action="reduce_frequency",
                params={"suggested_max": 30},
            )
        ],
        confidence=0.7,
    )


BUILTIN_RULES: List[AdviceRuleDefinition] = [
    AdviceRuleDefinition(
        id="high-failure-rate",
        app="*",
        type="risk",
        description="Detects operations with consistently high failure rates",
        evaluate=_rule_high_failure_rate,
    ),
    AdviceRuleDefinition(
        id="high-frequency",
        app="*",
        type="frequency",
        description="Detects unusually high activity frequency",
        evaluate=_rule_high_frequency,
    ),
]


# ---------------------------------------------------------------------------
# AdviceEngine
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


class AdviceEngine:
    def __init__(
        self,
        store_dir: Path,
        extra_rules: Optional[List[AdviceRuleDefinition]] = None,
    ):
        self._store_file = Path(store_dir) / "advices.json"
        raw = load_json(self._store_file, [])
        self._advices: List[AdviceEntry] = [
            AdviceEntry.from_dict(a) for a in raw if isinstance(a, dict)
        ]
        self._rules: List[AdviceRuleDefinition] = [
            *BUILTIN_RULES, *(extra_rules or [])
        ]

    @property
    def all(self) -> Sequence[AdviceEntry]:
        return tuple(self._advices)

    @property
    def size(self) -> int:
        return len(self._advices)

    def analyze(self, trace_store: Any, app: str) -> int:
        """Run rules over the trace store for *app*, append new advice.
        Returns count of new entries.
        """
        stats = self._build_stats(trace_store, app)
        generated = 0
        now = now_ms()
        for rule in self._rules:
            if rule.app != "*" and rule.app != app:
                continue
            candidate = rule.evaluate(stats)
            if candidate is None:
                continue
            # Dedup recent (within 24h) advice from the same rule
            duplicate = next(
                (a for a in self._advices
                 if a.app == app
                 and a.type == candidate.type
                 and a.summary == candidate.summary
                 and now - a.createdAt < 24 * 60 * 60 * 1000),
                None,
            )
            if duplicate is not None:
                continue
            entry = AdviceEntry(
                adviceId=_next_advice_id(),
                app=app,
                type=candidate.type,
                summary=candidate.summary,
                rules=list(candidate.rules),
                confidence=candidate.confidence,
                basedOnTraces=stats.totalTraces,
                createdAt=now,
                deliveredTo=[],
            )
            self._advices.append(entry)
            generated += 1
        if generated:
            self._save()
        return generated

    def get_pending_for_agent(
        self, agent_id: str, app: Optional[str] = None
    ) -> List[AdviceEntry]:
        now_s = now_ms() / 1000.0
        results: List[AdviceEntry] = []
        for a in self._advices:
            if agent_id in a.deliveredTo:
                continue
            if app and a.app != app:
                continue
            if a.expiresAt and _parse_iso(a.expiresAt) < now_s:
                continue
            results.append(a)
        return results

    def mark_delivered(self, advice_id: str, agent_id: str) -> None:
        entry = next(
            (a for a in self._advices if a.adviceId == advice_id), None
        )
        if entry is not None and agent_id not in entry.deliveredTo:
            entry.deliveredTo.append(agent_id)
            self._save()

    def get_for_app(self, app: str) -> List[AdviceEntry]:
        return [a for a in self._advices if a.app == app]

    # -- internal -----------------------------------------------------------

    def _build_stats(self, trace_store: Any, app: str) -> TraceStats:
        traces = trace_store.get_by_app(app)

        # operation rates
        op_groups: Dict[str, Dict[str, int]] = {}
        for t in traces:
            g = op_groups.setdefault(t.operation, {"success": 0, "total": 0})
            g["total"] += 1
            if t.success:
                g["success"] += 1
        operation_rates: Dict[str, Dict[str, float]] = {
            op: {
                "rate": (g["success"] / g["total"]) if g["total"] else 0.0,
                "total": g["total"],
            }
            for op, g in op_groups.items()
        }

        # traces per day (YYYY-MM-DD)
        traces_per_day: Dict[str, int] = {}
        for t in traces:
            day = (t.timestamp or "")[:10]
            if day:
                traces_per_day[day] = traces_per_day.get(day, 0) + 1

        # observed data aggregation
        observed_map: Dict[str, List[Any]] = {}
        for o in trace_store.extract_observed_data(app):
            for field_name, value in (o.get("data") or {}).items():
                observed_map.setdefault(field_name, []).append(value)
        observed_data = [
            {"field": name, "values": values}
            for name, values in observed_map.items()
        ]

        total = len(traces)
        success_count = sum(1 for t in traces if t.success)

        return TraceStats(
            app=app,
            totalTraces=total,
            successRate=(success_count / total) if total else 0.0,
            observedData=observed_data,
            operationRates=operation_rates,
            tracesPerDay=traces_per_day,
        )

    def _save(self) -> None:
        save_json(self._store_file, [a.to_dict() for a in self._advices])
