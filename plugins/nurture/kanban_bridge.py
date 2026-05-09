"""Bridge: community task → Hermes Kanban card.

Per ADR-007, community tasks execute via Hermes's Kanban worker fleet
rather than OpenClaw's hand-rolled subagent. When polling discovers a
new community task, this module:

1. Creates a Kanban card with the task body shaped as a worker prompt
2. Uses ``idempotency_key="nurture-task-<id>"`` so re-poll doesn't
   create duplicates
3. Records the kanban task id back into ``community-tasks.json`` so we
   can correlate worker outcome → community report

The worker (a hermes process launched by the dispatcher) sees the body
verbatim, calls ``nurture_execute`` to drive the device, and finishes
with ``nurture_task_report(taskId=...)`` — which the Phase 3 connector
forwards to the community (``forward_task_outcome``).

Worker → community task completion is therefore handled by the existing
nurture_task_report tool. This module is one-direction: community → kanban.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Default profile that picks up nurture community tasks. Users can
# override per-config; the bridge falls back to "nurture-task-worker".
_DEFAULT_WORKER_PROFILE = "nurture-task-worker"


def build_worker_prompt(task: Dict[str, Any]) -> str:
    """Construct the body the worker hermes will see as its opening turn.

    Mirrors the prompt OpenClaw constructed in ``dispatchTask`` (see
    OpenClaw ``community-connector.ts:dispatchTask``). The worker's
    nurture plugin ``pre_llm_call`` hook still injects persona /
    capabilities / static guidance per session — this body just
    delivers the task-specific facts.
    """
    task_id = task.get("taskId", "")
    app = task.get("app", "")
    description = task.get("description", "").strip()
    priority = task.get("priority", "normal")
    expires_at = task.get("expiresAt", "")

    parts = [
        f"[Community Task {task_id}] App: {app}",
        f"Description: {description}",
        "",
        f"Priority: {priority}; expires at {expires_at}.",
        "",
        "Instructions:",
        "1. Read <nurture-persona> and <nurture-goals> in your context. "
        "If this task conflicts with the persona, immediately call "
        "nurture_task_report(outcome=\"refused\") with a brief reason and stop.",
        "2. Otherwise, plan the operation steps using the operations listed "
        "in <nurture-capabilities> for this app, and execute them via the "
        "nurture_execute tool. The Python device service will record traces "
        "automatically; you do not need to manage them.",
        "3. When you are done (success / failed / partial / refused), "
        f"you MUST call nurture_task_report(taskId=\"{task_id}\", "
        "outcome=..., reason=..., summary=...) exactly once. The task is "
        "considered failed and will be retried if you forget to call it.",
        "4. Do not use exec/shell/adb directly. Do not invent tool names. "
        "Do not summarise without calling nurture_task_report.",
    ]
    return "\n".join(parts)


def build_kanban_title(task: Dict[str, Any]) -> str:
    """Short title shown in ``hermes kanban list`` and dashboards."""
    task_id = task.get("taskId", "")
    app = task.get("app", "")
    desc = task.get("description", "").strip()
    if len(desc) > 60:
        desc = desc[:57] + "..."
    return f"[Community Task {task_id}] {app}: {desc}"


def dispatch_to_kanban(
    task: Dict[str, Any],
    *,
    worker_profile: str = _DEFAULT_WORKER_PROFILE,
    board: Optional[str] = None,
) -> Optional[str]:
    """Create a Kanban card for *task*. Returns the new kanban task id,
    or None on failure.

    Idempotent: re-dispatching the same community task returns the same
    kanban task id (via ``idempotency_key``).
    """
    try:
        from hermes_cli import kanban_db as kb
    except ImportError as e:
        logger.warning("nurture: kanban_db unavailable: %s", e)
        return None

    nurture_task_id = task.get("taskId")
    if not nurture_task_id:
        logger.warning("nurture: task missing taskId, skipping kanban dispatch: %r", task)
        return None

    title = build_kanban_title(task)
    body = build_worker_prompt(task)
    priority_str = task.get("priority", "normal")
    priority_int = 10 if priority_str == "high" else 0

    # Skills hint to the worker — we ship a small skill that documents
    # the nurture worker contract. (Optional — Phase 6 may add
    # corresponding markdown to skills/ tree.)
    skills: List[str] = []

    try:
        with kb.connect(board=board) as conn:
            kanban_task_id = kb.create_task(
                conn,
                title=title,
                body=body,
                assignee=worker_profile,
                created_by="nurture-community-connector",
                workspace_kind="scratch",
                priority=priority_int,
                idempotency_key=f"nurture-task-{nurture_task_id}",
                skills=skills or None,
            )
    except Exception as e:
        logger.warning(
            "nurture: kanban dispatch failed for task %s: %s",
            nurture_task_id, e,
        )
        return None

    logger.info(
        "nurture: dispatched community task %s → kanban task %s (assignee=%s)",
        nurture_task_id, kanban_task_id, worker_profile,
    )
    return kanban_task_id


def maybe_dispatch_new_tasks(
    tasks_file: Path,
    *,
    worker_profile: str = _DEFAULT_WORKER_PROFILE,
    board: Optional[str] = None,
) -> int:
    """Scan ``community-tasks.json`` and dispatch any newly-arrived
    tasks (status=received, no kanban_task_id yet) to Kanban.

    Updates the file in place to record the kanban_task_id, and bumps
    status to "running". Returns the number of tasks dispatched this
    pass.
    """
    if not tasks_file.exists():
        return 0
    try:
        data = json.loads(tasks_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, list):
        return 0

    dispatched = 0
    changed = False
    for task in data:
        if not isinstance(task, dict):
            continue
        if task.get("status") != "received":
            continue
        if task.get("kanban_task_id"):
            continue  # already dispatched in a prior tick
        kanban_id = dispatch_to_kanban(
            task, worker_profile=worker_profile, board=board,
        )
        if kanban_id is None:
            continue
        task["kanban_task_id"] = kanban_id
        task["status"] = "running"
        dispatched += 1
        changed = True

    if changed:
        tasks_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return dispatched
