"""Hermes tools for device automation.

Registers two tools via ``ctx.register_tool``:

- ``nurture_execute`` — execute a device operation (Pi Agent's hot path).
  This is the LLM/no-LLM boundary documented in
  ``docs/avatar-hermes/boundary-contracts.md`` 不变量 1: every call here
  triggers ONE LLM round-trip; the underlying AdaptiveStep then runs
  Tier 0/1/2 entirely in the Python service with no further LLM.

- ``nurture_task_report`` — explicit outcome contract for community-
  dispatched tasks. Phase 1 stub that logs locally; Phase 4 (Kanban
  worker) will route the outcome back through the community connector.

Replaces ``extensions/nurture/src/device-tools.ts``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .device_bridge import DeviceBridge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cached providers (capabilities + persona) — avoid hitting Python every turn
# ---------------------------------------------------------------------------

class CachedCapabilityProvider:
    """30s TTL cache around DeviceBridge.get_capabilities().

    Returns last-good cache on transient errors so a momentary device
    service hiccup doesn't blank out the agent's capability context.
    """

    _TTL_S = 30.0

    def __init__(self, bridge: DeviceBridge) -> None:
        self._bridge = bridge
        self._lock = threading.Lock()
        self._cache: Optional[Dict[str, Any]] = None
        self._expiry: float = 0.0

    def get(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            if self._cache is not None and now < self._expiry:
                return self._cache
        try:
            fresh = self._bridge.get_capabilities()
        except Exception as e:
            logger.debug("nurture: capability fetch failed (using stale cache): %s", e)
            return self._cache
        with self._lock:
            self._cache = fresh
            self._expiry = time.monotonic() + self._TTL_S
            return fresh


class CachedPersonaProvider:
    """5min TTL cache around persona + goals reads.

    Persona / goals files are user-edited markdown; 5 minute staleness is
    fine. Caches all known apps in one shot.
    """

    _TTL_S = 300.0

    def __init__(self, bridge: DeviceBridge) -> None:
        self._bridge = bridge
        self._lock = threading.Lock()
        self._personas: Dict[str, Dict[str, Any]] = {}
        self._goals: str = ""
        self._expiry: float = 0.0

    def get(self, apps: List[str]) -> Dict[str, Any]:
        """Returns ``{personas: {app: {exists, raw}}, goals: str}``."""
        with self._lock:
            now = time.monotonic()
            if now < self._expiry:
                return {"personas": dict(self._personas), "goals": self._goals}

        personas: Dict[str, Dict[str, Any]] = {}
        for app in apps:
            try:
                p = self._bridge.get_persona(app)
                personas[app] = {"exists": bool(p.get("exists")), "raw": p.get("raw") or ""}
            except Exception:
                pass  # Service may be down — skip silently
        try:
            goals_resp = self._bridge.get_goals()
            goals = goals_resp.get("raw") or ""
        except Exception:
            goals = ""

        with self._lock:
            self._personas = personas
            self._goals = goals
            self._expiry = time.monotonic() + self._TTL_S
            return {"personas": dict(personas), "goals": goals}


# ---------------------------------------------------------------------------
# nurture_execute — device operation tool
# ---------------------------------------------------------------------------

NURTURE_EXECUTE_SCHEMA: Dict[str, Any] = {
    "name": "nurture_execute",
    "description": (
        "Execute a device operation on a connected Android device via the local "
        "Python device service. Refer to the <nurture-capabilities> block in your "
        "context to choose the right operation. Operations with has_recipe=true "
        "are deterministic RPA (fast, ~3s); has_recipe=false uses VLM fallback "
        "(slower but exploratory)."
    ),
    "parameters": {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "description": (
                    "Operation ID to execute, e.g. 'douyin.enter_data_center'. "
                    "Use the capabilities block to find available operations."
                ),
            },
            "device_id": {
                "type": "string",
                "description": (
                    "Target device serial (from `adb devices`). "
                    "If omitted, auto-selects the first online device."
                ),
            },
            "params": {
                "type": "object",
                "description": "Operation-specific parameters (most operations work with defaults).",
            },
        },
    },
}


def make_nurture_execute_handler(
    bridge: DeviceBridge,
    bound_device_id: Optional[str],
) -> Callable[..., str]:
    """Build the handler closure with bridge + bound device id captured."""

    def handler(args: Dict[str, Any], **_kwargs: Any) -> str:
        operation = str(args.get("operation") or "").strip()
        if not operation:
            return json.dumps({"error": "operation is required"})

        # Reject community protocol commands — those go through MCP tools.
        if operation.startswith("nurture."):
            return json.dumps({
                "error": (
                    f"'{operation}' is a community protocol command, not a device "
                    f"operation. Use the nurture.* MCP tools instead. Device "
                    f"operations look like 'douyin.enter_recommend_feed'."
                )
            })

        device_id = str(args.get("device_id") or "").strip()
        params_raw = args.get("params")
        params = params_raw if isinstance(params_raw, dict) else {}

        # Resolve target device
        if not device_id:
            if bound_device_id:
                device_id = bound_device_id
                logger.info("nurture: using bound device %s", device_id)
            else:
                try:
                    devices = bridge.list_devices()
                except Exception as e:
                    return json.dumps({"error": f"Failed to list devices: {e}"})
                online = [d for d in devices if d.get("status") == "device"]
                if not online:
                    return json.dumps({
                        "error": "No online Android devices found. Check USB/ADB connection.",
                    })
                device_id = online[0].get("serial", "")
                logger.info("nurture: auto-selected device %s", device_id)

        logger.info("nurture: executing operation=%s device=%s", operation, device_id)
        t0 = time.monotonic()
        try:
            result = bridge.execute(operation=operation, device_id=device_id, params=params)
        except Exception as e:
            logger.warning("nurture: execute failed: %s", e)
            return json.dumps({"error": f"Failed to execute {operation}: {e}"})

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "nurture: operation=%s http_roundtrip=%dms",
            operation, elapsed_ms,
        )
        return json.dumps(result, ensure_ascii=False)

    return handler


# ---------------------------------------------------------------------------
# nurture_task_report — community task outcome contract
# ---------------------------------------------------------------------------

VALID_OUTCOMES = ("success", "failed", "refused", "partial")

NURTURE_TASK_REPORT_SCHEMA: Dict[str, Any] = {
    "name": "nurture_task_report",
    "description": (
        "Report the final outcome of a community-dispatched task. You MUST call "
        "this exactly once at the end of any community task run, regardless of "
        "outcome (success / failed / refused / partial). Not calling it makes "
        "the task count as failed and it will be retried on the next poll."
    ),
    "parameters": {
        "type": "object",
        "required": ["taskId", "outcome", "reason"],
        "properties": {
            "taskId": {
                "type": "string",
                "description": "Community task id from the dispatched message (e.g. 'task-...').",
            },
            "outcome": {
                "type": "string",
                "enum": list(VALID_OUTCOMES),
                "description": (
                    "Final outcome: 'success' = goal achieved; 'failed' = attempted "
                    "but tools/device errored; 'refused' = persona conflict, did not "
                    "attempt; 'partial' = some progress but stopped early."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Short human-readable explanation. For non-success outcomes "
                    "this is mandatory and is forwarded to the community for analytics."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "Optional summary of what was actually done (steps taken, "
                    "observations). Helps future runs and trace analysis."
                ),
            },
        },
    },
}


def make_nurture_task_report_handler(
    forward_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Callable[..., str]:
    """Phase 1: log locally + best-effort forward (no community wiring yet).

    Phase 4 (Kanban worker) will pass a ``forward_fn`` that translates the
    outcome into ``kanban_complete`` / ``kanban_block``.
    """

    def handler(args: Dict[str, Any], **_kwargs: Any) -> str:
        task_id = str(args.get("taskId") or "").strip()
        outcome = str(args.get("outcome") or "").strip()
        reason = str(args.get("reason") or "").strip()
        summary_raw = args.get("summary")
        summary = summary_raw.strip() if isinstance(summary_raw, str) and summary_raw.strip() else None

        if not task_id:
            return json.dumps({"error": "taskId is required"})
        if outcome not in VALID_OUTCOMES:
            return json.dumps({
                "error": f"outcome must be one of {VALID_OUTCOMES} (got {outcome!r})"
            })
        if not reason:
            return json.dumps({"error": "reason is required"})

        payload = {"taskId": task_id, "outcome": outcome, "reason": reason}
        if summary:
            payload["summary"] = summary

        logger.info(
            "nurture_task_report: task=%s outcome=%s reason=%r",
            task_id, outcome, reason,
        )

        # Phase 1 stub: forward_fn is None until Phase 3+ wires the
        # community connector / kanban bridge.
        if forward_fn is not None:
            try:
                forward_fn(payload)
            except Exception as e:
                logger.warning("nurture_task_report: forward failed: %s", e)

        return json.dumps({
            "ok": True,
            "recorded_locally": True,
            "forwarded": forward_fn is not None,
            **payload,
        }, ensure_ascii=False)

    return handler
