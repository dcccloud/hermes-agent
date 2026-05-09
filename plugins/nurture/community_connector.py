"""Background thread that drives the device→community sync loop.

Six tasks on staggered ticks:
  - 60s: capability upload (response carries bestRecipes → cache)
  - 60s: trace upload
  - 60s: event upload
  - 30s: tasks poll  → community-tasks.json
  - 30s: advices poll
  - 30s: directives poll

Plus on first run: register avatar (or refresh if expiring within 7d)
and call ``register_mcp_servers`` so Pi Agent automatically sees
``nurture.recipes.get`` / ``nurture.graph.get`` / ``nurture.event.query``.

Replaces OpenClaw's 4 setInterval timers + GatewayClient.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from .community_poll import poll_advices, poll_directives, poll_tasks
from .community_sync import (
    SyncState,
    build_capability_payload,
    build_event_payload,
    build_trace_payload,
    post_upload,
    write_best_recipes_cache,
)
from .community_token import CommunityToken, TokenStore, ensure_token
from .config import NurtureConfig
from .device_bridge import DeviceBridge
from .kanban_bridge import maybe_dispatch_new_tasks

logger = logging.getLogger(__name__)


_TICK_INTERVAL_S = 5.0  # how often the loop wakes up to check schedules
_UPLOAD_INTERVAL_S = 60.0
_POLL_INTERVAL_S = 30.0


class CommunityConnector:
    """Owns the background sync thread + MCP client registration.

    Construction is cheap (no I/O). Call :meth:`start` from the plugin
    on_session_start hook (idempotent). Call :meth:`stop` to drain.
    """

    def __init__(self, config: NurtureConfig, bridge: DeviceBridge) -> None:
        self._config = config
        self._bridge = bridge
        self._state = SyncState()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._token: Optional[CommunityToken] = None
        self._token_store = TokenStore(
            config.workspace_path / "agent" / "community-token.json"
        )

    @property
    def is_offline(self) -> bool:
        return not self._config.community.url

    def start(self) -> bool:
        """Start the background thread + register MCP server.

        Idempotent. Returns True if the thread is now running. Returns
        False if the plugin is configured offline (no community.url).
        """
        if self.is_offline:
            logger.debug("nurture: connector offline (community.url empty)")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True

        # Register / refresh avatar token (sync, blocks startup briefly)
        self._token = ensure_token(
            store=self._token_store,
            community_url=self._config.community.url,
            agent_id=self._config.agent_id or "agent-default",
            device_id=self._config.device_id or "",
            device_model="",
            platform="android",
        )
        if self._token is None:
            logger.warning(
                "nurture: connector starting in degraded mode (no token, community offline?)"
            )

        # Register MCP server URL with hermes — Pi Agent auto-discovers
        # nurture.* tools the next time model_tools is consulted.
        if self._token is not None:
            self._register_mcp()

        # Start the polling/upload thread
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="nurture-community-connector",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "nurture: connector started (community=%s, agentId=%s, avatarId=%s)",
            self._config.community.url,
            self._config.agent_id or "(auto)",
            self._token.avatarId if self._token else "(none)",
        )
        return True

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # -- MCP client registration ------------------------------------------

    def _register_mcp(self) -> None:
        try:
            from tools.mcp_tool import register_mcp_servers
        except ImportError as e:
            logger.warning("nurture: hermes mcp_tool unavailable: %s", e)
            return
        if self._token is None:
            return
        try:
            tool_names = register_mcp_servers({
                "nurture-community": {
                    "url": f"{self._config.community.url.rstrip('/')}/mcp",
                    "headers": {
                        "Authorization": f"Bearer {self._token.token}",
                    },
                    "timeout": 30,
                    "connect_timeout": 10,
                }
            })
            logger.info(
                "nurture: registered community MCP server, tools available: %s",
                tool_names,
            )
        except Exception as e:
            logger.warning("nurture: register_mcp_servers failed: %s", e)

    # -- background loop --------------------------------------------------

    def _loop(self) -> None:
        last_caps = 0.0
        last_trace = 0.0
        last_event = 0.0
        last_task = 0.0
        last_advice = 0.0
        last_directive = 0.0

        while not self._stop_event.is_set():
            now = time.monotonic()

            # Refresh token if needed
            if self._token is None or self._token.is_expiring_soon():
                self._token = ensure_token(
                    store=self._token_store,
                    community_url=self._config.community.url,
                    agent_id=self._config.agent_id or "agent-default",
                    device_id=self._config.device_id or "",
                    device_model="",
                    platform="android",
                )
                if self._token is not None:
                    self._register_mcp()

            if self._token is None:
                # Still offline — sleep and retry
                self._stop_event.wait(_TICK_INTERVAL_S)
                continue

            try:
                if now - last_caps >= _UPLOAD_INTERVAL_S:
                    self._upload_capability()
                    last_caps = now

                if now - last_trace >= _UPLOAD_INTERVAL_S:
                    self._upload_traces()
                    last_trace = now

                if now - last_event >= _UPLOAD_INTERVAL_S:
                    self._upload_events()
                    last_event = now

                if now - last_task >= _POLL_INTERVAL_S:
                    self._poll_tasks()
                    last_task = now

                if now - last_advice >= _POLL_INTERVAL_S:
                    self._poll_advices()
                    last_advice = now

                if now - last_directive >= _POLL_INTERVAL_S:
                    self._poll_directives()
                    last_directive = now
            except Exception:
                logger.exception("nurture: connector loop tick failed")

            self._stop_event.wait(_TICK_INTERVAL_S)

    # -- per-task helpers -------------------------------------------------

    def _upload_capability(self) -> None:
        if self._token is None:
            return
        caps = build_capability_payload(self._bridge)
        if caps is None:
            return
        result = post_upload(
            community_url=self._config.community.url,
            token=self._token.token,
            kind="capability",
            agent_id=self._config.agent_id or "agent-default",
            device_id=self._config.device_id or "",
            payload=caps,
        )
        if result is None:
            return
        if result.get("_status") == 401:
            self._token = None
            return
        best_recipes = result.get("bestRecipes") or []
        if best_recipes:
            write_best_recipes_cache(
                workspace=self._config.workspace_path,
                best_recipes=best_recipes,
            )

    def _upload_traces(self) -> None:
        if self._token is None:
            return
        traces = build_trace_payload(
            self._bridge, self._state,
            agent_id=self._config.agent_id or "agent-default",
        )
        if not traces:
            return
        post_upload(
            community_url=self._config.community.url,
            token=self._token.token,
            kind="trace",
            agent_id=self._config.agent_id or "agent-default",
            device_id=self._config.device_id or "",
            payload={"traces": traces},
        )

    def _upload_events(self) -> None:
        if self._token is None:
            return
        events, new_cursor = build_event_payload(
            self._bridge, self._state,
            agent_id=self._config.agent_id or "agent-default",
        )
        if events:
            post_upload(
                community_url=self._config.community.url,
                token=self._token.token,
                kind="event",
                agent_id=self._config.agent_id or "agent-default",
                device_id=self._config.device_id or "",
                payload={"events": events},
            )
        self._state.event_cursor = new_cursor

    def _poll_tasks(self) -> None:
        if self._token is None:
            return
        result = poll_tasks(
            community_url=self._config.community.url,
            token=self._token.token,
            workspace=self._config.workspace_path,
        )
        if result is None:
            self._token = None  # will trigger refresh on next tick if 401
            return

        # Phase 4: dispatch any newly-arrived community tasks to Kanban
        # so a worker hermes profile picks them up. The bridge is
        # idempotent (idempotency_key=nurture-task-<id>) so re-poll
        # without status reset is a no-op.
        try:
            tasks_file = self._config.workspace_path / "agent" / "community-tasks.json"
            dispatched = maybe_dispatch_new_tasks(
                tasks_file,
                worker_profile=(
                    self._config.community.worker_profile
                    or "nurture-task-worker"
                ),
            )
            if dispatched:
                logger.info(
                    "nurture: dispatched %d new community task(s) to Kanban",
                    dispatched,
                )
        except Exception as e:
            logger.warning("nurture: kanban dispatch failed: %s", e)

    def _poll_advices(self) -> None:
        if self._token is None:
            return
        poll_advices(
            community_url=self._config.community.url,
            token=self._token.token,
            workspace=self._config.workspace_path,
        )

    def _poll_directives(self) -> None:
        if self._token is None:
            return
        poll_directives(
            community_url=self._config.community.url,
            token=self._token.token,
            workspace=self._config.workspace_path,
        )

    # -- task outcome forward ---------------------------------------------

    def forward_task_outcome(self, payload: dict) -> bool:
        """Post a ``nurture_task_report`` outcome to /api/task/complete.

        Used by the device_tools handler in Phase 4 to notify the
        community when a community task finishes. Returns True on
        success, False on any error (best-effort).
        """
        if self._token is None or self.is_offline:
            return False
        import httpx
        url = f"{self._config.community.url.rstrip('/')}/api/task/complete"
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    url, json=payload,
                    headers={"Authorization": f"Bearer {self._token.token}"},
                )
        except httpx.RequestError as e:
            logger.warning("nurture: forward task outcome failed: %s", e)
            return False
        if resp.status_code >= 400:
            logger.warning(
                "nurture: forward task outcome returned %d: %s",
                resp.status_code, resp.text[:200],
            )
            return False
        return True
