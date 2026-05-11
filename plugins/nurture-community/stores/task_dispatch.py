"""TaskDispatchEngine (JSON backend) — bulletin-board for community tasks.

Tasks are published with requirements; qualifying agents see them and
execute autonomously. Lifecycle: active → completed | expired.

PG backend: ``plugins/nurture-community/pg/pg_task_dispatch.py``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence

from ._helpers import load_json, now_ms, save_json


TaskStatus = Literal["active", "completed", "expired"]
Priority = Literal["normal", "high"]


@dataclass
class TaskRequirements:
    app: str
    platform: List[str] = field(default_factory=list)
    minCapabilities: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskRequirements":
        return cls(
            app=data.get("app", ""),
            platform=list(data.get("platform") or []),
            minCapabilities=list(data.get("minCapabilities") or []),
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"app": self.app}
        if self.platform:
            d["platform"] = self.platform
        if self.minCapabilities:
            d["minCapabilities"] = self.minCapabilities
        return d


@dataclass
class TaskDispatch:
    taskId: str
    requirements: TaskRequirements
    app: str
    description: str
    priority: Priority
    createdAt: int
    expiresAt: str
    status: TaskStatus
    completedAt: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskDispatch":
        return cls(
            taskId=data.get("taskId", ""),
            requirements=TaskRequirements.from_dict(data.get("requirements") or {}),
            app=data.get("app", ""),
            description=data.get("description", ""),
            priority=data.get("priority", "normal") or "normal",
            createdAt=int(data.get("createdAt", 0) or 0),
            expiresAt=data.get("expiresAt", ""),
            status=data.get("status", "active") or "active",
            completedAt=data.get("completedAt"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "taskId": self.taskId,
            "requirements": self.requirements.to_dict(),
            "app": self.app,
            "description": self.description,
            "priority": self.priority,
            "createdAt": self.createdAt,
            "expiresAt": self.expiresAt,
            "status": self.status,
        }
        if self.completedAt is not None:
            d["completedAt"] = self.completedAt
        return d


# Module-level monotonic counter for taskId generation. Matches OpenClaw's
# `let taskIdCounter = 0; function nextTaskId() { return ${Date.now()}-${++counter}; }`.
_task_id_counter = count(1)


def _next_task_id() -> str:
    return f"task-{now_ms()}-{next(_task_id_counter)}"


def _parse_iso(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


class TaskDispatchEngine:
    def __init__(self, store_dir: Path):
        self._store_file = Path(store_dir) / "task-dispatch.json"
        self._last_mtime: float = 0.0
        self._tasks: List[TaskDispatch] = []
        self._reload()

    def _reload(self) -> None:
        """Read the JSON file from disk + cache mtime."""
        raw = load_json(self._store_file, [])
        self._tasks = [
            TaskDispatch.from_dict(t) for t in raw if isinstance(t, dict)
        ]
        try:
            self._last_mtime = self._store_file.stat().st_mtime
        except OSError:
            self._last_mtime = 0.0

    def _maybe_reload(self) -> None:
        """Multi-process safety: re-read when another process (e.g.
        ``hermes nurture-community tasks create`` via the CLI) wrote
        the file. Cheap mtime stat; reload only on change.
        """
        try:
            mtime = self._store_file.stat().st_mtime
        except OSError:
            return
        if mtime > self._last_mtime:
            self._reload()

    @property
    def all(self) -> Sequence[TaskDispatch]:
        return tuple(self._tasks)

    @property
    def size(self) -> int:
        return len(self._tasks)

    def create(
        self,
        *,
        requirements: TaskRequirements,
        app: str,
        description: str,
        priority: Priority = "normal",
        expires_at: Optional[str] = None,
    ) -> TaskDispatch:
        """Create a new active task. Default expiry: 1 hour from now."""
        if not expires_at:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat().replace("+00:00", "Z")
        task = TaskDispatch(
            taskId=_next_task_id(),
            requirements=requirements,
            app=app,
            description=description,
            priority=priority,
            createdAt=now_ms(),
            expiresAt=expires_at,
            status="active",
        )
        self._tasks.append(task)
        self._save()
        return task

    def get(self, task_id: str) -> Optional[TaskDispatch]:
        self._maybe_reload()
        return next((t for t in self._tasks if t.taskId == task_id), None)

    def get_available(
        self, *, platform: Optional[str] = None, capabilities: Optional[List[str]] = None
    ) -> List[TaskDispatch]:
        """Return active, non-expired tasks matching the avatar."""
        self._maybe_reload()
        now_s = now_ms() / 1000.0
        results: List[TaskDispatch] = []
        for t in self._tasks:
            if t.status != "active":
                continue
            if _parse_iso(t.expiresAt) < now_s:
                continue
            req = t.requirements
            if platform and req.platform and platform not in req.platform:
                continue
            if capabilities and req.minCapabilities:
                if not all(cap in capabilities for cap in req.minCapabilities):
                    continue
            results.append(t)
        return results

    def complete(self, task_id: str) -> Optional[TaskDispatch]:
        task = self.get(task_id)
        if task is None or task.status != "active":
            return None
        task.status = "completed"
        task.completedAt = now_ms()
        self._save()
        return task

    def query(
        self,
        *,
        status: Optional[TaskStatus] = None,
        app: Optional[str] = None,
    ) -> List[TaskDispatch]:
        self._maybe_reload()
        results = self._tasks
        if status:
            results = [t for t in results if t.status == status]
        if app:
            results = [t for t in results if t.app == app]
        return list(results)

    def clean_expired(self) -> int:
        """Mark active tasks past their expiresAt as expired. Returns count."""
        now_s = now_ms() / 1000.0
        expired = 0
        for task in self._tasks:
            if task.status == "active" and _parse_iso(task.expiresAt) < now_s:
                task.status = "expired"
                expired += 1
        if expired:
            self._save()
        return expired

    def prune(self, max_age_ms: int = 7 * 24 * 3_600_000) -> int:
        """Drop non-active tasks older than *max_age_ms*. Returns count removed."""
        cutoff = now_ms() - max_age_ms
        before = len(self._tasks)
        self._tasks = [
            t for t in self._tasks
            if t.status == "active" or t.createdAt >= cutoff
        ]
        removed = before - len(self._tasks)
        if removed:
            self._save()
        return removed

    def _save(self) -> None:
        save_json(self._store_file, [t.to_dict() for t in self._tasks])
