"""Polling for tasks / advices / directives from the community.

Replaces OpenClaw's WebSocket push events with simple HTTP GET. Each
30-second tick hits all three endpoints and writes the results into
local JSON files that the Python device service / future Kanban worker
will consume.

State files (matching OpenClaw schema where possible):
  ``$HERMES_HOME/nurture/agent/community-tasks.json``
  ``$HERMES_HOME/nurture/agent/advice.json``
  ``$HERMES_HOME/nurture/agent/directives.json``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


def _get(
    *, community_url: str, token: str, path: str, timeout: float = 30.0
) -> Optional[Any]:
    url = f"{community_url.rstrip('/')}{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.RequestError as e:
        logger.debug("nurture: poll %s failed: %s", path, e)
        return None
    if resp.status_code == 401:
        logger.warning("nurture: poll %s got 401 — token may need refresh", path)
        return {"_status": 401}
    if resp.status_code >= 400:
        logger.warning(
            "nurture: poll %s returned %d: %s",
            path, resp.status_code, resp.text[:200],
        )
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        return None


def poll_tasks(
    *, community_url: str, token: str, workspace: Path
) -> Optional[List[Dict[str, Any]]]:
    """Poll active tasks. Merges with local file (preserves status/outcome
    fields populated by the worker)."""
    body = _get(community_url=community_url, token=token, path="/api/tasks/poll")
    if body is None or (isinstance(body, dict) and body.get("_status") == 401):
        return None
    if not isinstance(body, dict):
        return None
    tasks = body.get("tasks") or []
    cache_path = workspace / "agent" / "community-tasks.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Merge: preserve local fields (status / outcome / failureReason / reportedAt)
    # for taskIds we already know about; add new ones; drop tasks no longer in
    # the active list.
    existing = _load_json_list(cache_path)
    by_id_local = {
        t.get("taskId"): t for t in existing if isinstance(t, dict) and t.get("taskId")
    }
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            continue
        task_id = t.get("taskId") or ""
        if not task_id:
            continue
        seen.add(task_id)
        local = by_id_local.get(task_id, {})
        merged.append({
            **t,
            "receivedAt": local.get("receivedAt") or _now_ms(),
            "status": local.get("status", "received"),
            **{
                k: v for k, v in {
                    "outcome": local.get("outcome"),
                    "failureReason": local.get("failureReason"),
                    "summary": local.get("summary"),
                    "reportedAt": local.get("reportedAt"),
                }.items() if v is not None
            },
        })

    cache_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return merged


def poll_advices(
    *, community_url: str, token: str, workspace: Path
) -> Optional[List[Dict[str, Any]]]:
    body = _get(community_url=community_url, token=token, path="/api/advices/poll")
    if body is None or (isinstance(body, dict) and body.get("_status") == 401):
        return None
    if not isinstance(body, dict):
        return None
    advices = body.get("advices") or []
    cache_path = workspace / "agent" / "advice.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(advices, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return advices


def poll_directives(
    *, community_url: str, token: str, workspace: Path
) -> Optional[List[Dict[str, Any]]]:
    body = _get(community_url=community_url, token=token, path="/api/directives/poll")
    if body is None or (isinstance(body, dict) and body.get("_status") == 401):
        return None
    if not isinstance(body, dict):
        return None
    directives = body.get("directives") or []
    cache_path = workspace / "agent" / "directives.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(directives, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return directives


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json_list(path: Path) -> List[Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)
