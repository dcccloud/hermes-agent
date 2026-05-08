"""Operation-level checkpoint persistence for resumable task execution.

Tracks completed operations per task so that on task resubmission (after a
timeout, crash, or partial completion), already-finished operations can be
skipped while preserving their results.

Each task has its own JSONL file:
    extensions/nurture/data/checkpoints/{task_id}.jsonl

A separate index file tracks the (fingerprint -> task_id) mapping so the
service can recover task identity when the caller does not provide an
explicit task_id:
    extensions/nurture/data/checkpoints/_index.jsonl

Both files are append-only; the latest record for a key wins.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("checkpoint_store")

_DATA_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "data" / "checkpoints"
)

# Recognized status values for op records. Stored as plain strings; the
# resume logic only treats "success" as "skip on resume".
OP_STATUS_SUCCESS = "success"
OP_STATUS_FAILED = "failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def compute_op_params_hash(params: Dict[str, Any] | None) -> str:
    """Stable hash over op params, used to detect signature drift on resume."""
    canonical = json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def compute_task_fingerprint(
    device_id: str, operations: List[Dict[str, Any]]
) -> str:
    """Stable fingerprint over (device, operations) for task identity reuse.

    Same device + same operations (by name and params) within the TTL
    window is treated as the same task — its existing task_id is reused
    so checkpoints / in-flight traces can be picked up.
    """
    canonical_ops = [
        {"name": op.get("name", ""), "params": op.get("params", {}) or {}}
        for op in (operations or [])
    ]
    canonical = json.dumps(
        {"device_id": device_id, "operations": canonical_ops},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class CheckpointEntry:
    """A single op-level checkpoint record."""
    task_id: str
    op_index: int
    op_name: str
    op_params_hash: str
    device_id: str
    status: str       # OP_STATUS_SUCCESS / OP_STATUS_FAILED
    result: Dict[str, Any]
    started_at: str
    completed_at: str

    @classmethod
    def from_record(cls, rec: Dict[str, Any]) -> "CheckpointEntry":
        return cls(
            task_id=rec["task_id"],
            op_index=int(rec["op_index"]),
            op_name=rec.get("op_name", ""),
            op_params_hash=rec.get("op_params_hash", ""),
            device_id=rec.get("device_id", ""),
            status=rec.get("status", OP_STATUS_FAILED),
            result=rec.get("result", {}) or {},
            started_at=rec.get("started_at", ""),
            completed_at=rec.get("completed_at", ""),
        )


class CheckpointStore:
    """Append-only JSONL store for op-level task checkpoints."""

    def __init__(self, data_dir: str | Path | None = None):
        self._dir = Path(data_dir) if data_dir else _DATA_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._dir

    def _task_file(self, task_id: str) -> Path:
        # task_id may contain characters unsafe for filesystem paths
        safe = task_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe}.jsonl"

    def _index_file(self) -> Path:
        return self._dir / "_index.jsonl"

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def register_task(
        self,
        task_id: str,
        fingerprint: str,
        device_id: str,
        operations_count: int = 0,
    ) -> None:
        """Record a new task in the index. Safe to call multiple times."""
        record = {
            "task_id": task_id,
            "fingerprint": fingerprint,
            "device_id": device_id,
            "operations_count": operations_count,
            "registered_at": _now_iso(),
        }
        with open(self._index_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # Also seed the per-task file with a start marker so the file
        # exists on disk before any op completes.
        marker = {
            "type": "task_start",
            "task_id": task_id,
            "fingerprint": fingerprint,
            "device_id": device_id,
            "operations_count": operations_count,
            "started_at": _now_iso(),
        }
        with open(self._task_file(task_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(marker, ensure_ascii=False) + "\n")

    def find_by_fingerprint(
        self,
        fingerprint: str,
        max_age_seconds: int = 1800,
        device_id: Optional[str] = None,
    ) -> Optional[str]:
        """Return the most recently registered task_id with this fingerprint
        inside the TTL window, or ``None`` if no fresh match exists.
        """
        idx = self._index_file()
        if not idx.exists():
            return None
        cutoff = _now_ts() - max_age_seconds
        best_ts = -1.0
        best_id: Optional[str] = None
        with open(idx, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("fingerprint") != fingerprint:
                    continue
                if device_id and rec.get("device_id") != device_id:
                    continue
                ts = self._parse_ts(rec.get("registered_at"))
                if ts is None or ts < cutoff:
                    continue
                if ts > best_ts:
                    best_ts = ts
                    best_id = rec.get("task_id")
        return best_id

    # ------------------------------------------------------------------
    # Op-level checkpoints
    # ------------------------------------------------------------------

    def record_op(
        self,
        task_id: str,
        op_index: int,
        op_name: str,
        status: str,
        result: Dict[str, Any] | None = None,
        device_id: str = "",
        op_params_hash: str = "",
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        """Persist an op-level checkpoint record (append)."""
        record = {
            "type": "op",
            "task_id": task_id,
            "op_index": op_index,
            "op_name": op_name,
            "op_params_hash": op_params_hash,
            "device_id": device_id,
            "status": status,
            "result": result or {},
            "started_at": started_at or _now_iso(),
            "completed_at": completed_at or _now_iso(),
        }
        path = self._task_file(task_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        logger.info(
            "Checkpoint saved: task=%s op_index=%d op=%s status=%s",
            task_id, op_index, op_name, status,
        )

    def get_task(self, task_id: str) -> List[CheckpointEntry]:
        """Return latest op-level checkpoint entries for a task, sorted
        by op_index. The latest record per op_index wins (so re-runs of
        the same op overwrite the previous outcome).
        """
        path = self._task_file(task_id)
        if not path.exists():
            return []
        latest: Dict[int, Dict[str, Any]] = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "op":
                    continue
                idx = rec.get("op_index")
                if idx is None:
                    continue
                latest[int(idx)] = rec
        return [
            CheckpointEntry.from_record(latest[i])
            for i in sorted(latest.keys())
        ]

    def get_completed_ops(
        self, task_id: str
    ) -> Dict[int, CheckpointEntry]:
        """Return ``{op_index: entry}`` for ops that completed successfully."""
        return {
            entry.op_index: entry
            for entry in self.get_task(task_id)
            if entry.status == OP_STATUS_SUCCESS
        }

    def compute_resume_point(
        self,
        task_id: str,
        requested_ops: List[Dict[str, Any]],
        strict: bool = False,
    ) -> Dict[str, Any]:
        """Inspect existing checkpoints and decide where to resume.

        Returns a dict::

            {
              "skip_indices": {0, 1, ...},       # ops to skip with cached result
              "skip_results": {0: result, ...},  # cached op_result for each skip
              "first_unmatched": Optional[int],  # first op where signature drifted
              "mismatch": bool,                  # any signature mismatch detected
            }

        Behavior:
          - Walk requested_ops in order; for each op_index we have a
            successful checkpoint AND the (op_name, op_params_hash) match,
            mark it skippable.
          - First mismatch (or first index without a checkpoint) stops
            the skip walk; everything from there on runs fresh.
          - When ``strict`` is True and any mismatch is detected, the
            entire skip set is cleared (caller should restart from 0).
        """
        completed = self.get_completed_ops(task_id)
        skip_indices: set[int] = set()
        skip_results: Dict[int, Dict[str, Any]] = {}
        first_unmatched: Optional[int] = None
        mismatch = False

        for idx, op_config in enumerate(requested_ops):
            entry = completed.get(idx)
            if entry is None:
                first_unmatched = idx
                break
            req_name = op_config.get("name", "")
            req_hash = compute_op_params_hash(op_config.get("params", {}))
            if entry.op_name != req_name or (
                entry.op_params_hash and entry.op_params_hash != req_hash
            ):
                mismatch = True
                first_unmatched = idx
                logger.warning(
                    "Checkpoint signature mismatch at op_index=%d: "
                    "checkpoint=(%s,%s) requested=(%s,%s)",
                    idx, entry.op_name, entry.op_params_hash,
                    req_name, req_hash,
                )
                break
            skip_indices.add(idx)
            skip_results[idx] = entry.result

        if mismatch and strict:
            skip_indices.clear()
            skip_results.clear()
            first_unmatched = 0

        return {
            "skip_indices": skip_indices,
            "skip_results": skip_results,
            "first_unmatched": first_unmatched,
            "mismatch": mismatch,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ts(raw: str | None) -> float | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None
