"""Layer 2 strategy intelligence — analytics aggregator and reports.

Phase 5 MVP scope (per ADR-003):
  - aggregator.py: cron-triggered trace aggregation across all apps
  - weekly_report.py: markdown summary delivered via hermes multi-platform

Layer 2 in OpenClaw is "what works best" (when to post / what to like /
what frequency); the MVP here builds the plumbing so the community can
*observe* and *push* recommendations. Full A/B framework, trust-rating,
and cross-app pattern mining are out of scope.

See docs/avatar-hermes/community-agent.md §3 for the full design.
"""

from .aggregator import (
    AggregatedReport,
    AppMetrics,
    OperationMetrics,
    aggregate_all,
    aggregate_app_metrics,
)
from .weekly_report import build_weekly_report

__all__ = [
    "AggregatedReport",
    "AppMetrics",
    "OperationMetrics",
    "aggregate_all",
    "aggregate_app_metrics",
    "build_weekly_report",
]
