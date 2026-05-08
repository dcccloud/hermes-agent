"""Unified event log — append-only JSONL store for all device-side events.

Records operation lifecycle, step execution, page discovery, recipe evolution,
and other significant events in chronological order. Complements TraceStore
(which stores full VLM action traces for RecipeGenerator) with lightweight
event metadata for community reporting, debugging, and analytics.

Storage: ``data/events/YYYY-MM-DD.jsonl`` (one file per day, 14-day retention).
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("event_log")

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "events"

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: "EventLog | None" = None
_instance_lock = threading.Lock()


def get_event_log() -> "EventLog":
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = EventLog()
    return _instance


def emit(
    event_type: str,
    payload: dict[str, Any],
    *,
    device_id: str = "",
    operation: str | None = None,
    step: str | None = None,
) -> str:
    """Module-level convenience. Auto-reads task_id from thread-local."""
    from openclaw_agent.engine.common import get_task_id

    return get_event_log().emit(
        event_type=event_type,
        payload=payload,
        device_id=device_id,
        task_id=get_task_id() or None,
        operation=operation,
        step=step,
    )


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------


class EventLog:
    """Append-only JSONL event log with per-day file rotation."""

    def __init__(self, data_dir: str | Path | None = None):
        self._dir = Path(data_dir) if data_dir else _DATA_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        # Per-file locks to allow concurrent writes to different date files
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def _get_lock(self, filename: str) -> threading.Lock:
        if filename not in self._locks:
            with self._locks_lock:
                if filename not in self._locks:
                    self._locks[filename] = threading.Lock()
        return self._locks[filename]

    def _today_file(self) -> Path:
        name = datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"
        return self._dir / name

    # -- Write ---------------------------------------------------------------

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        device_id: str = "",
        task_id: str | None = None,
        operation: str | None = None,
        step: str | None = None,
    ) -> str:
        """Append one event. Returns the event id. Thread-safe."""
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        record: dict[str, Any] = {
            "id": event_id,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device_id": device_id,
        }
        if task_id:
            record["task_id"] = task_id
        if operation:
            record["operation"] = operation
        if step:
            record["step"] = step
        record["payload"] = payload

        path = self._today_file()
        lock = self._get_lock(path.name)
        with lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return event_id

    # -- Read ----------------------------------------------------------------

    def query(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        event_types: list[str] | None = None,
        task_id: str | None = None,
        operation: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Read events matching filters. Returns (events, next_cursor).

        Cursor format: ``filename:byte_offset`` for resumable pagination.
        """
        limit = min(limit, 500)
        files = sorted(self._dir.glob("*.jsonl"))
        if not files:
            return [], None

        # Determine starting file and byte offset from cursor
        start_file: str | None = None
        start_offset: int = 0
        if cursor:
            parts = cursor.split(":", 1)
            if len(parts) == 2:
                start_file = parts[0]
                start_offset = int(parts[1])

        results: list[dict[str, Any]] = []
        next_cursor: str | None = None

        for path in files:
            # Skip files before cursor file
            if start_file and path.name < start_file:
                continue

            # Skip files outside date range (filename is YYYY-MM-DD.jsonl)
            file_date = path.stem  # "2026-04-05"
            if since and file_date < since[:10]:
                continue
            if until and file_date > until[:10]:
                continue

            offset = start_offset if path.name == start_file else 0

            with open(path, "r", encoding="utf-8") as f:
                if offset > 0:
                    f.seek(offset)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Apply filters
                    if since and rec.get("timestamp", "") < since:
                        continue
                    if until and rec.get("timestamp", "") > until:
                        continue
                    if event_types and rec.get("event_type") not in event_types:
                        continue
                    if task_id and rec.get("task_id") != task_id:
                        continue
                    if operation and rec.get("operation") != operation:
                        continue

                    results.append(rec)
                    if len(results) >= limit:
                        # Record cursor at current position
                        next_cursor = f"{path.name}:{f.tell()}"
                        return results, next_cursor

            # Reset start_offset after processing the cursor file
            start_offset = 0

        return results, None

    # -- Maintenance ---------------------------------------------------------

    def retention_prune(self, max_days: int = 14) -> int:
        """Delete JSONL files older than max_days. Returns files removed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).strftime(
            "%Y-%m-%d"
        )
        removed = 0
        for path in sorted(self._dir.glob("*.jsonl")):
            if path.stem < cutoff:
                try:
                    path.unlink()
                    removed += 1
                    logger.info(f"Pruned old event log: {path.name}")
                except OSError:
                    pass
        return removed
