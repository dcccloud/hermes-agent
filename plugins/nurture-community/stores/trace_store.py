"""TraceStore (JSON backend) — community-level storage for agent
behavior traces.

Receives traces reported by agents (via /api/upload?kind=trace),
indexes them by app/operation/agentId, provides query APIs for the
AdviceEngine to analyze patterns. Circular buffer with default 10k
capacity.

PG backend: see ``plugins/nurture-community/pg/pg_trace_store.py``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ._helpers import load_json, now_ms, save_json


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TraceAction:
    type: str
    target: Optional[str] = None
    result: Optional[str] = None
    observed_data: Optional[Dict[str, Any]] = None
    screenshot_hash: Optional[str] = None
    timestamp: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TraceAction":
        return cls(
            type=data.get("type", ""),
            target=data.get("target"),
            result=data.get("result"),
            observed_data=data.get("observed_data"),
            screenshot_hash=data.get("screenshot_hash"),
            timestamp=data.get("timestamp"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # drop None to keep JSON minimal
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class TraceEntry:
    traceId: str
    agentId: str
    deviceId: str
    app: str
    operation: str
    step: str
    timestamp: str
    actions: List[TraceAction]
    success: bool
    ingestedAt: int
    duration_ms: Optional[int] = None
    taskId: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TraceEntry":
        actions_raw = data.get("actions") or []
        return cls(
            traceId=data.get("traceId", ""),
            agentId=data.get("agentId", ""),
            deviceId=data.get("deviceId", ""),
            app=data.get("app", ""),
            operation=data.get("operation", ""),
            step=data.get("step", ""),
            timestamp=data.get("timestamp", ""),
            actions=[
                TraceAction.from_dict(a) for a in actions_raw if isinstance(a, dict)
            ],
            success=bool(data.get("success", False)),
            ingestedAt=int(data.get("ingestedAt", 0) or 0),
            duration_ms=data.get("duration_ms"),
            taskId=data.get("taskId"),
            extra=data.get("extra"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "traceId": self.traceId,
            "agentId": self.agentId,
            "deviceId": self.deviceId,
            "app": self.app,
            "operation": self.operation,
            "step": self.step,
            "timestamp": self.timestamp,
            "actions": [a.to_dict() for a in self.actions],
            "success": self.success,
            "ingestedAt": self.ingestedAt,
        }
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        if self.taskId is not None:
            d["taskId"] = self.taskId
        if self.extra is not None:
            d["extra"] = self.extra
        return d


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> float:
    """Parse ISO timestamp to seconds since epoch; 0 on failure."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0.0


class TraceStore:
    def __init__(self, store_dir: Path, *, max_traces: int = 10_000):
        self._store_file = Path(store_dir) / "traces.json"
        self._max_traces = max_traces
        raw = load_json(self._store_file, [])
        self._traces: List[TraceEntry] = [
            TraceEntry.from_dict(t) for t in raw if isinstance(t, dict)
        ]

    @property
    def all(self) -> Sequence[TraceEntry]:
        return tuple(self._traces)

    @property
    def size(self) -> int:
        return len(self._traces)

    def ingest(self, entry: Dict[str, Any]) -> TraceEntry:
        """Ingest a trace. Deduplicates by ``traceId``; evicts oldest on overflow."""
        trace_id = entry.get("traceId", "")
        for existing in self._traces:
            if existing.traceId == trace_id:
                return existing

        full = TraceEntry.from_dict({**entry, "ingestedAt": now_ms()})
        self._traces.append(full)
        if len(self._traces) > self._max_traces:
            self._traces = self._traces[-self._max_traces :]

        self._save()
        return full

    # -- queries ------------------------------------------------------------

    def get_by_app(self, app: str) -> List[TraceEntry]:
        return [t for t in self._traces if t.app == app]

    def get_by_operation(self, app: str, operation: str) -> List[TraceEntry]:
        return [
            t for t in self._traces if t.app == app and t.operation == operation
        ]

    def get_by_agent(self, agent_id: str) -> List[TraceEntry]:
        return [t for t in self._traces if t.agentId == agent_id]

    def get_by_task_id(self, task_id: str) -> List[TraceEntry]:
        return [t for t in self._traces if t.taskId == task_id]

    def get_by_time_range(self, since: str, until: Optional[str] = None) -> List[TraceEntry]:
        since_ts = _parse_iso(since)
        until_ts = _parse_iso(until) if until else float("inf")
        return [
            t for t in self._traces if since_ts <= _parse_iso(t.timestamp) <= until_ts
        ]

    def get_recent(self, limit: int = 50) -> List[TraceEntry]:
        return list(reversed(self._traces[-limit:]))

    def extract_observed_data(
        self,
        app: str,
        *,
        since: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Pull all observed_data fields from traces for *app*, useful for
        AdviceEngine analysis."""
        filtered = self.get_by_app(app)
        if since:
            since_ts = _parse_iso(since)
            filtered = [t for t in filtered if _parse_iso(t.timestamp) >= since_ts]
        if agent_id:
            filtered = [t for t in filtered if t.agentId == agent_id]

        results: List[Dict[str, Any]] = []
        for trace in filtered:
            for action in trace.actions:
                if action.observed_data:
                    results.append({
                        "traceId": trace.traceId,
                        "agentId": trace.agentId,
                        "timestamp": trace.timestamp,
                        "data": action.observed_data,
                    })
        return results

    def get_success_rate(self, app: str, operation: str) -> Dict[str, float]:
        traces = self.get_by_operation(app, operation)
        if not traces:
            return {"rate": 0.0, "total": 0}
        successes = sum(1 for t in traces if t.success)
        return {"rate": successes / len(traces), "total": len(traces)}

    # -- internal -----------------------------------------------------------

    def _save(self) -> None:
        save_json(self._store_file, [t.to_dict() for t in self._traces])
