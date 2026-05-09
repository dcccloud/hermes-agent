"""Trace aggregator — produces operational metrics for Layer 2.

Run via ``hermes nurture-community analyze`` or scheduled with
``hermes cron`` (see docs/avatar-hermes/community-agent.md §3.4).

Output is a structured ``AggregatedReport`` covering:
  - Per-app totals (traces, success rate, distinct agents)
  - Per-operation success rates and avg duration
  - Recipe coverage (how many ops have a recipe)
  - Daily volume distribution

The report is fed to:
  1. ``advice_engine.analyze`` — already runs the rule engine over
     trace data on each call; this module just exposes the same view
     in a friendlier shape for humans / dashboards.
  2. ``weekly_report.build_weekly_report`` — markdown summary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..knowledge_engine import KnowledgeEngine


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class OperationMetrics:
    """Per-operation aggregation."""
    operation: str
    total_traces: int = 0
    success_count: int = 0
    avg_duration_ms: float = 0.0
    has_recipe: bool = False
    recipe_global_rate: float = 0.0  # if has_recipe, the best-recipe success rate

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_traces if self.total_traces else 0.0


@dataclass
class AppMetrics:
    """Per-app rollup."""
    app: str
    total_traces: int = 0
    success_count: int = 0
    distinct_agents: int = 0
    distinct_devices: int = 0
    operations: Dict[str, OperationMetrics] = field(default_factory=dict)
    daily_volume: Dict[str, int] = field(default_factory=dict)  # YYYY-MM-DD → count
    consensus_pages: int = 0  # graph states with >=2 confirms

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_traces if self.total_traces else 0.0

    @property
    def recipe_coverage(self) -> float:
        """Fraction of operations that have at least one recipe."""
        if not self.operations:
            return 0.0
        with_recipe = sum(1 for op in self.operations.values() if op.has_recipe)
        return with_recipe / len(self.operations)


@dataclass
class AggregatedReport:
    """Full aggregation across the community."""
    generated_at: int  # unix-ms
    window_start: int  # unix-ms (inclusive)
    window_end: int    # unix-ms (exclusive)
    apps: Dict[str, AppMetrics] = field(default_factory=dict)

    @property
    def total_traces(self) -> int:
        return sum(a.total_traces for a in self.apps.values())

    @property
    def total_agents(self) -> int:
        # Distinct agents may overlap across apps — caller should pass a
        # set if they need a precise count. For the rollup here, sum the
        # per-app numbers (slight overcounting is acceptable for the
        # weekly-report headline).
        return sum(a.distinct_agents for a in self.apps.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "apps": {
                name: {
                    "total_traces": m.total_traces,
                    "success_rate": m.success_rate,
                    "distinct_agents": m.distinct_agents,
                    "distinct_devices": m.distinct_devices,
                    "consensus_pages": m.consensus_pages,
                    "recipe_coverage": m.recipe_coverage,
                    "operations": {
                        op_name: {
                            "total_traces": o.total_traces,
                            "success_rate": o.success_rate,
                            "avg_duration_ms": o.avg_duration_ms,
                            "has_recipe": o.has_recipe,
                            "recipe_global_rate": o.recipe_global_rate,
                        }
                        for op_name, o in m.operations.items()
                    },
                    "daily_volume": m.daily_volume,
                }
                for name, m in self.apps.items()
            },
        }


# ---------------------------------------------------------------------------
# Aggregation logic
# ---------------------------------------------------------------------------


def _parse_iso_to_ms(s: str) -> Optional[int]:
    try:
        return int(
            datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000
        )
    except (ValueError, TypeError, AttributeError):
        return None


async def aggregate_app_metrics(
    engine: KnowledgeEngine,
    app: str,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> AppMetrics:
    """Build per-app aggregation for *app* over the given window.

    Defaults to the last 7 days when *since*/*until* are not supplied.
    """
    now = datetime.now(timezone.utc)
    since = since or (now - timedelta(days=7))
    until = until or now
    since_ms = int(since.timestamp() * 1000)
    until_ms = int(until.timestamp() * 1000)

    metrics = AppMetrics(app=app)

    traces = await engine.get_traces_by_app(app)
    distinct_agents: set[str] = set()
    distinct_devices: set[str] = set()
    op_groups: Dict[str, List[Any]] = {}

    for t in traces:
        ts_ms = _parse_iso_to_ms(t.timestamp)
        if ts_ms is None or ts_ms < since_ms or ts_ms >= until_ms:
            continue
        metrics.total_traces += 1
        if t.success:
            metrics.success_count += 1
        if t.agentId:
            distinct_agents.add(t.agentId)
        if t.deviceId:
            distinct_devices.add(t.deviceId)
        op_groups.setdefault(t.operation, []).append(t)
        day = (t.timestamp or "")[:10]
        if day:
            metrics.daily_volume[day] = metrics.daily_volume.get(day, 0) + 1

    metrics.distinct_agents = len(distinct_agents)
    metrics.distinct_devices = len(distinct_devices)

    # Per-operation rollups + recipe lookup
    for op_name, op_traces in op_groups.items():
        success = sum(1 for t in op_traces if t.success)
        durations = [t.duration_ms for t in op_traces if t.duration_ms]
        avg_dur = sum(durations) / len(durations) if durations else 0.0
        op_metric = OperationMetrics(
            operation=op_name,
            total_traces=len(op_traces),
            success_count=success,
            avg_duration_ms=avg_dur,
        )
        # Look up canonical "main" recipe — rough proxy for "has_recipe"
        best = await engine.get_best_recipe(app, op_name, "main")
        if best is not None and best.code:
            op_metric.has_recipe = True
            op_metric.recipe_global_rate = best.globalSuccessRate
        metrics.operations[op_name] = op_metric

    # Consensus pages
    consensus = await engine.get_consensus_graph_states(app)
    metrics.consensus_pages = len(consensus)

    return metrics


async def aggregate_all(
    engine: KnowledgeEngine,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> AggregatedReport:
    """Aggregate metrics across all apps tracked by the community."""
    now = datetime.now(timezone.utc)
    since = since or (now - timedelta(days=7))
    until = until or now

    # Discover apps from the trace store: any trace for an app means the
    # app is "tracked". We don't have a public list_apps() yet so we
    # walk a known set; the community server has avatar-registry that
    # carries app names too. For now, scan via raw store access since
    # JSON backend's `all` is exposed.
    apps_seen: set[str] = set()
    if not engine.is_pg:
        # JSON backend — walk the trace store directly
        for t in engine.trace_store.all:
            if t.app:
                apps_seen.add(t.app)
    else:
        # PG backend — query via SQL (any traces ever touched)
        async with engine.trace_store._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT app FROM nurture_traces"
            )
        for r in rows:
            apps_seen.add(r["app"])

    report = AggregatedReport(
        generated_at=int(now.timestamp() * 1000),
        window_start=int(since.timestamp() * 1000),
        window_end=int(until.timestamp() * 1000),
    )
    for app in sorted(apps_seen):
        metrics = await aggregate_app_metrics(
            engine, app, since=since, until=until,
        )
        if metrics.total_traces > 0:
            report.apps[app] = metrics
    return report
