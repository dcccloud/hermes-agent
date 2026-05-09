"""VLM action-trace persistence.

Each trace captures the full step-by-step actions executed by the VLM
during a single fallback run, keyed by ``(operation, step)``.
Traces are stored as JSONL files grouped by operation name.

Every record carries an explicit ``outcome`` so the store doubles as
an event log:

  - ``completed``        — VLM finished and passed verification
  - ``max_steps``        — VLM ran out of steps without ``finish``;
                           a later trace may continue this one via
                           ``continuation_of``
  - ``vlm_error``        — VLM raised before finishing
  - ``verify_failed``    — VLM finished but verify_fn rejected
  - ``all_tiers_failed`` — recipe + VLM both failed (AdaptiveStep)
  - ``in_flight``        — task is still running (or was killed mid-op);
                           reserved for future per-op markers

Downstream consumers filter by outcome; ``get_pending`` defaults to
``completed`` so RecipeGenerator/Reviewer behavior stays unchanged.

This module also provides ``InFlightTraceWriter`` for resumable
execution: VLM step entries are appended to ``data/in_flight/{task_id}.jsonl``
in real time so that a process kill / network timeout never loses the
exploration history. On task resume, ``read_in_flight_steps(task_id)``
returns the prior step trail as the source for VLM prior-context
injection.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("trace_store")

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "traces"
# In-flight per-step trail lives outside the traces dir so it is NOT
# scanned by the existing recipe-learning consumers.
_IN_FLIGHT_DIR = _DATA_DIR.parent / "in_flight"
_IN_FLIGHT_ARCHIVE_DIR = _DATA_DIR.parent / "in_flight_archive"

# Recognized outcome values. Stored as plain strings; this set is for
# documentation and light validation only.
OUTCOMES = (
    "completed",
    "max_steps",
    "vlm_error",
    "verify_failed",
    "all_tiers_failed",
    "in_flight",
)


class TraceStore:
    """Append-only JSONL store for VLM action traces."""

    def __init__(self, data_dir: str | Path | None = None):
        self._dir = Path(data_dir) if data_dir else _DATA_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def _file_for(self, operation: str) -> Path:
        path = self._dir / f"{operation}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def save(
        self,
        operation: str,
        step: str,
        trace: list[dict[str, Any]],
        device_id: str = "",
        extra: dict[str, Any] | None = None,
        task_id: str | None = None,
        outcome: str = "completed",
        prompt: str | None = None,
        continuation_of: str | None = None,
    ) -> str:
        """Persist a single VLM trace. Returns the trace id."""
        trace_id = uuid.uuid4().hex[:12]
        record: dict[str, Any] = {
            "id": trace_id,
            "operation": operation,
            "step": step,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device_id": device_id,
            "actions": trace,
            "outcome": outcome,
            "consumed": False,
        }
        if task_id:
            record["task_id"] = task_id
        if prompt:
            record["prompt"] = prompt
        if continuation_of:
            record["continuation_of"] = continuation_of
        if extra:
            record["extra"] = extra

        path = self._file_for(operation)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.info(
            f"Trace saved: op={operation} step={step} "
            f"id={trace_id} outcome={outcome} actions={len(trace)}"
        )
        return trace_id

    def get_pending(
        self,
        operation: str | None = None,
        step: str | None = None,
        limit: int = 20,
        outcomes: Iterable[str] | None = None,
        include_consumed: bool = False,
    ) -> list[dict[str, Any]]:
        """Return traces, optionally filtered.

        ``outcomes`` defaults to ``("completed",)`` so existing
        consumers (RecipeGenerator, RecipeReviewer) only see successful
        traces. Pass an explicit iterable (or empty/None-equivalent
        sentinel ``"*"``) to broaden the query.

        When *include_consumed* is True, already-consumed traces are
        included in the results (used by community trace reporting).
        """
        wanted = self._resolve_outcomes(outcomes)
        results: list[dict[str, Any]] = []
        files = (
            [self._file_for(operation)]
            if operation
            else sorted(self._dir.rglob("*.jsonl"))
        )
        for path in files:
            if not path.exists():
                continue
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not include_consumed and rec.get("consumed"):
                    continue
                if wanted is not None and rec.get("outcome", "completed") not in wanted:
                    continue
                if step and rec.get("step") != step:
                    continue
                results.append(rec)
                if len(results) >= limit:
                    return results
        return results

    def find_recent(
        self,
        device_id: str,
        outcomes: Iterable[str],
        max_age_seconds: int,
        operation_prefix: str | None = None,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` recent records matching the filter,
        newest first.

        Used by the NL continuation path to find the most recent
        ``max_steps`` trace for a device. ``operation_prefix`` (e.g.
        ``"douyin/"``) scopes the scan to a single app's files.
        """
        wanted = self._resolve_outcomes(outcomes) or set()
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds

        if operation_prefix:
            base = self._dir / operation_prefix.rstrip("/")
            files = sorted(base.rglob("*.jsonl")) if base.exists() else []
        else:
            files = sorted(self._dir.rglob("*.jsonl"))

        candidates: list[dict[str, Any]] = []
        for path in files:
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("device_id") != device_id:
                    continue
                if rec.get("outcome", "completed") not in wanted:
                    continue
                ts = self._parse_ts(rec.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
                rec["_ts"] = ts
                candidates.append(rec)

        candidates.sort(key=lambda r: r["_ts"], reverse=True)
        for c in candidates:
            c.pop("_ts", None)
        return candidates[:limit]

    def mark_consumed(self, trace_ids: set[str]) -> None:
        """Mark traces as consumed by rewriting the JSONL files."""
        if not trace_ids:
            return
        for path in sorted(self._dir.rglob("*.jsonl")):
            lines: list[str] = []
            changed = False
            for raw in open(path, encoding="utf-8"):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    lines.append(raw)
                    continue
                if rec.get("id") in trace_ids:
                    rec["consumed"] = True
                    changed = True
                lines.append(json.dumps(rec, ensure_ascii=False))
            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")

    @staticmethod
    def _resolve_outcomes(outcomes: Iterable[str] | None) -> set[str] | None:
        """Normalize the outcomes filter.

        - ``None`` (default) → only ``completed``
        - ``"*"`` or an iterable containing ``"*"`` → no filter
        - any other iterable → use as-is
        """
        if outcomes is None:
            return {"completed"}
        s = set(outcomes)
        if "*" in s:
            return None
        return s

    @staticmethod
    def _parse_ts(raw: str | None) -> float | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None

    def find_by_task(
        self,
        task_id: str,
        max_age_seconds: int = 7200,
        outcomes: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return completed-op trace records for a given task_id.

        Used by the resume path to pull every per-op trace tagged with
        ``task_id`` so we can build a comprehensive prior-attempt
        summary spanning all ops that actually finished saving traces.
        Newest first.
        """
        wanted = self._resolve_outcomes(outcomes if outcomes is not None else "*")
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
        results: list[dict[str, Any]] = []
        for path in sorted(self._dir.rglob("*.jsonl")):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("task_id") != task_id:
                    continue
                if wanted is not None and rec.get("outcome", "completed") not in wanted:
                    continue
                ts = self._parse_ts(rec.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
                rec["_ts"] = ts
                results.append(rec)
        results.sort(key=lambda r: r["_ts"], reverse=True)
        for r in results:
            r.pop("_ts", None)
        return results


# ---------------------------------------------------------------------------
# In-flight per-step trace (resumable execution)
# ---------------------------------------------------------------------------


class InFlightTraceWriter:
    """Append-only writer for VLM single-step trace entries.

    One file per ``task_id`` accumulates every VLM step taken across all
    ops in that task run. The file persists between process restarts
    so that a task killed mid-VLM (timeout, crash) leaves a complete
    record on disk for the next attempt to consume.

    Thread-safe enough for single-process append (line-buffered writes).
    """

    def __init__(
        self,
        task_id: str,
        device_id: str = "",
        base_dir: str | Path | None = None,
    ):
        self.task_id = task_id
        self.device_id = device_id
        self._dir = Path(base_dir) if base_dir else _IN_FLIGHT_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        safe = task_id.replace("/", "_").replace("\\", "_")
        self._path = self._dir / f"{safe}.jsonl"
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def append_step(
        self,
        op_index: int,
        op_name: str,
        step_entry: dict[str, Any],
    ) -> None:
        """Persist one VLM step. Best-effort — exceptions are swallowed
        so a write failure never breaks the running VLM.
        """
        if self._closed:
            return
        record = {
            "task_id": self.task_id,
            "op_index": op_index,
            "op_name": op_name,
            "device_id": self.device_id,
            "step": step_entry,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.exception(
                "InFlightTraceWriter append failed task=%s op_index=%d",
                self.task_id, op_index,
            )

    def close(self) -> None:
        """Mark this writer closed (further appends become no-ops)."""
        self._closed = True


def read_in_flight_steps(
    task_id: str,
    base_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return all in-flight step records for a task, in append order.

    Returns ``[]`` if no file exists for this task.
    """
    base = Path(base_dir) if base_dir else _IN_FLIGHT_DIR
    safe = task_id.replace("/", "_").replace("\\", "_")
    path = base / f"{safe}.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def archive_in_flight(
    task_id: str,
    base_dir: str | Path | None = None,
    archive_dir: str | Path | None = None,
) -> bool:
    """Move the in-flight trace file to the archive dir. Returns ``True``
    if a file was archived, ``False`` if no file existed.
    """
    base = Path(base_dir) if base_dir else _IN_FLIGHT_DIR
    archive = Path(archive_dir) if archive_dir else _IN_FLIGHT_ARCHIVE_DIR
    safe = task_id.replace("/", "_").replace("\\", "_")
    src = base / f"{safe}.jsonl"
    if not src.exists():
        return False
    archive.mkdir(parents=True, exist_ok=True)
    dst = archive / f"{safe}.jsonl"
    # If dst already exists (re-archive), append a timestamp suffix
    if dst.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dst = archive / f"{safe}.{ts}.jsonl"
    src.rename(dst)
    logger.info("Archived in-flight trace task=%s -> %s", task_id, dst)
    return True
