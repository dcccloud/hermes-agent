"""Builders + uploaders for the device → community batch upload path.

Three kinds of batch uploads (all hit ``POST /api/upload``):
  - capability — full capability snapshot, ~60s cadence; response carries bestRecipes
  - trace      — pending VLM traces, ~60s cadence
  - event      — incremental events, ~60s cadence (cursor-based)

This module is sync (httpx.Client) so the background thread in
``community_connector.py`` can drive it from a plain ``while`` loop
without an event loop.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .device_bridge import DeviceBridge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cursor / dedup state (in-memory, session-scoped)
# ---------------------------------------------------------------------------


class SyncState:
    """Cursors and dedup sets for the current process lifetime."""

    def __init__(self) -> None:
        self.event_cursor: Optional[str] = None
        self.reported_trace_ids: set[str] = set()


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def build_capability_payload(bridge: DeviceBridge) -> Optional[Dict[str, Any]]:
    """Pull capabilities from the local Python service."""
    try:
        caps = bridge.get_capabilities()
    except Exception as e:
        logger.debug("nurture: capability fetch failed: %s", e)
        return None
    if not isinstance(caps, dict):
        return None
    return caps


def build_trace_payload(
    bridge: DeviceBridge,
    state: SyncState,
    *,
    agent_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Pull pending traces, dedup against session-scoped reported set."""
    try:
        raw = bridge.get_pending_traces(limit=limit)
    except Exception as e:
        logger.debug("nurture: trace fetch failed: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        trace_id = r.get("id") or r.get("traceId")
        if not trace_id or trace_id in state.reported_trace_ids:
            continue
        # Translate to community schema
        op = r.get("operation", "")
        app = op.split("/", 1)[0] if "/" in op else r.get("app", "") or ""
        out.append({
            "traceId": trace_id,
            "agentId": agent_id,
            "deviceId": r.get("device_id", "") or "",
            "app": app,
            "operation": op,
            "step": r.get("step", "") or "",
            "timestamp": r.get("timestamp", ""),
            "actions": _normalize_actions(r.get("actions") or []),
            "success": bool(r.get("success", False)),
            "duration_ms": r.get("duration_ms"),
            "taskId": r.get("task_id"),
            "extra": r.get("extra"),
        })
        state.reported_trace_ids.add(trace_id)
    return out


def build_event_payload(
    bridge: DeviceBridge,
    state: SyncState,
    *,
    agent_id: str,
    limit: int = 100,
    event_types: str = (
        "operation.complete,step.complete,state.discovered,"
        "recipe.generated,recipe.promoted,recipe.disabled,"
        "recipe.rolled_back,recipe.community_adopted"
    ),
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Pull incremental events; returns (events, new_cursor)."""
    try:
        result = bridge.get_events(
            cursor=state.event_cursor, limit=limit, event_types=event_types,
        )
    except Exception as e:
        logger.debug("nurture: event fetch failed: %s", e)
        return [], state.event_cursor
    raw_events = result.get("events") or []
    out: List[Dict[str, Any]] = []
    for e in raw_events:
        if not isinstance(e, dict):
            continue
        out.append({
            "eventId": e.get("event_id") or e.get("eventId") or "",
            "agentId": agent_id,
            "deviceId": e.get("device_id", "") or "",
            "eventType": e.get("event_type") or e.get("eventType") or "",
            "timestamp": e.get("timestamp", ""),
            "operation": e.get("operation", "") or "",
            "step": e.get("step", "") or "",
            "taskId": e.get("task_id"),
            "payload": e.get("payload") or {},
        })
    new_cursor = result.get("cursor") or state.event_cursor
    return out, new_cursor


def _normalize_actions(actions: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        out.append({
            "type": a.get("type") or a.get("action") or "unknown",
            "target": a.get("target"),
            "result": a.get("result"),
            "observed_data": a.get("observed_data"),
            "screenshot_hash": a.get("screenshot_hash"),
            "timestamp": a.get("timestamp"),
        })
    return out


# ---------------------------------------------------------------------------
# Upload + best-recipe cache
# ---------------------------------------------------------------------------


def post_upload(
    *,
    community_url: str,
    token: str,
    kind: str,
    agent_id: str,
    device_id: str,
    payload: Dict[str, Any],
    timeout: float = 60.0,
) -> Optional[Dict[str, Any]]:
    """POST a batch to ``/api/upload``. Returns the response body or None
    on transport error."""
    url = f"{community_url.rstrip('/')}/api/upload"
    body = {
        "kind": kind,
        "agentId": agent_id,
        "deviceId": device_id,
        "timestamp": _now_seconds(),
        "payload": payload,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url, json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.RequestError as e:
        logger.debug("nurture: upload %s failed: %s", kind, e)
        return None
    if resp.status_code == 401:
        logger.warning("nurture: upload %s got 401 — token may need refresh", kind)
        return {"_status": 401}
    if resp.status_code >= 400:
        logger.warning(
            "nurture: upload %s returned %d: %s", kind, resp.status_code, resp.text[:200]
        )
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        return None


def write_best_recipes_cache(
    *,
    workspace: Path,
    best_recipes: List[Dict[str, Any]],
) -> None:
    """Persist community-pushed best recipes to the cache the Python
    device service watches (matches OpenClaw schema)."""
    if not best_recipes:
        return
    cache_path = workspace / "agent" / "community-recipes-cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            "updatedAt": int(_now_seconds() * 1000),
            "recipes": best_recipes,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _now_seconds() -> float:
    import time
    return time.time()
