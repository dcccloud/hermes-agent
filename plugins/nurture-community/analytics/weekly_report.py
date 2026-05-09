"""Weekly report builder — markdown summary for human / chat consumption.

Designed to be delivered by Hermes cron + multi-platform send_message::

    hermes cron create "0 9 * * 1" \\
      "Generate Avatar-Hermes weekly report" \\
      --script ~/.hermes/scripts/nurture-weekly-report.py \\
      --deliver telegram \\
      --name "Nurture weekly report"

The script (``scripts/nurture-weekly-report.py`` in the repo) is a
thin wrapper that:
  1. Loads the community KnowledgeEngine (JSON or PG)
  2. Calls :func:`build_weekly_report`
  3. Prints the markdown to stdout (cron pipes it to delivery target)

Empty / quiet weeks output a one-line ``[SILENT]`` so cron's
``[SILENT]``-pattern delivery skips the notification.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from ..knowledge_engine import KnowledgeEngine
from .aggregator import AppMetrics, AggregatedReport, aggregate_all


_SILENT_TAG = "[SILENT]"


async def build_weekly_report(engine: KnowledgeEngine) -> str:
    """Aggregate the past 7 days and return a markdown summary string.

    Returns ``[SILENT]`` (a string starting with the silent tag) when
    the window has zero activity — useful for cron's silent-skip
    delivery pattern.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    report = await aggregate_all(engine, since=since, until=now)

    if report.total_traces == 0:
        return _SILENT_TAG + " no community activity in the past 7 days"

    lines: List[str] = []
    lines.append(f"# Avatar-Hermes weekly report")
    lines.append(f"_window: {since.date()} → {now.date()}_")
    lines.append("")
    lines.append(f"- Total traces: **{report.total_traces:,}**")
    lines.append(f"- Apps active: **{len(report.apps)}**")
    lines.append(f"- Distinct agents (sum across apps): **{report.total_agents}**")
    lines.append("")

    for app_name in sorted(report.apps):
        metrics = report.apps[app_name]
        lines.extend(_format_app_section(metrics))
        lines.append("")

    # Top regressions / wins across all apps
    risks = _surface_risks(report)
    if risks:
        lines.append("## Risks")
        lines.extend(risks)
        lines.append("")

    wins = _surface_wins(report)
    if wins:
        lines.append("## Wins")
        lines.extend(wins)
        lines.append("")

    return "\n".join(lines)


def _format_app_section(metrics: AppMetrics) -> List[str]:
    lines = [f"## {metrics.app}"]
    lines.append(
        f"- Traces: {metrics.total_traces:,} • "
        f"Success rate: **{metrics.success_rate * 100:.1f}%** • "
        f"Distinct agents: {metrics.distinct_agents}"
    )
    lines.append(
        f"- Recipe coverage: {metrics.recipe_coverage * 100:.0f}% "
        f"({sum(1 for o in metrics.operations.values() if o.has_recipe)}/"
        f"{len(metrics.operations)} ops) • "
        f"Consensus pages: {metrics.consensus_pages}"
    )

    # Top 5 ops by volume
    top_ops = sorted(
        metrics.operations.values(),
        key=lambda o: o.total_traces,
        reverse=True,
    )[:5]
    if top_ops:
        lines.append("")
        lines.append("**Top operations by volume:**")
        for o in top_ops:
            recipe_tag = "✓" if o.has_recipe else "✗"
            lines.append(
                f"- `{o.operation}` — {o.total_traces} traces, "
                f"{o.success_rate * 100:.1f}% success, "
                f"{o.avg_duration_ms:.0f}ms avg [{recipe_tag} recipe]"
            )
    return lines


def _surface_risks(report: AggregatedReport) -> List[str]:
    """Operations with >=10 traces and <50% success rate."""
    risks: List[str] = []
    for app_name, metrics in report.apps.items():
        for op in metrics.operations.values():
            if op.total_traces >= 10 and op.success_rate < 0.5:
                risks.append(
                    f"- `{app_name}/{op.operation}`: "
                    f"{op.success_rate * 100:.1f}% over {op.total_traces} traces"
                )
    return risks


def _surface_wins(report: AggregatedReport) -> List[str]:
    """Operations with >=10 traces and >=95% success rate (mature recipes)."""
    wins: List[str] = []
    for app_name, metrics in report.apps.items():
        for op in metrics.operations.values():
            if op.total_traces >= 10 and op.success_rate >= 0.95 and op.has_recipe:
                wins.append(
                    f"- `{app_name}/{op.operation}`: "
                    f"{op.success_rate * 100:.1f}% over {op.total_traces} traces "
                    f"(recipe global rate {op.recipe_global_rate * 100:.0f}%)"
                )
    return wins
