"""nurture plugin — Avatar-Hermes device automation.

Phase 3: Python device service spawn + 2 tools + pre_llm_call hook +
community connector (MCP client + REST polling background thread).

Design: see ``docs/avatar-hermes/`` (README, decisions, boundary-contracts,
device-agent, mcp-http-protocol).

Phase 4 will add: Kanban worker bridge for community task execution.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .cli import register_cli as _register_nurture_cli
from .cli import nurture_command as _nurture_command
from .cli import slash_handler as _slash_handler
from .community_connector import CommunityConnector
from .config import NurtureConfig
from .device_bridge import DeviceBridge
from .device_tools import (
    CachedCapabilityProvider,
    CachedPersonaProvider,
    NURTURE_EXECUTE_SCHEMA,
    NURTURE_TASK_REPORT_SCHEMA,
    make_nurture_execute_handler,
    make_nurture_task_report_handler,
)
from .prompt_hook import make_pre_llm_call_hook
from .service import start_python_service, stop_python_service

logger = logging.getLogger(__name__)


# Module-level singletons set on first session start. Re-used across
# sessions to share the bridge HTTP client + capability cache + connector.
_state: dict[str, Any] = {
    "config": None,
    "bridge": None,
    "cap_provider": None,
    "persona_provider": None,
    "connector": None,
    "started": False,
}


def _ensure_state(config: NurtureConfig) -> None:
    if _state["bridge"] is None:
        bridge = DeviceBridge(config.server_host, config.server_port)
        _state["bridge"] = bridge
        _state["cap_provider"] = CachedCapabilityProvider(bridge)
        _state["persona_provider"] = CachedPersonaProvider(bridge)
    _state["config"] = config


def _on_session_start(**_kwargs: Any) -> None:
    """Spawn Python device service + community connector (idempotent).

    Errors are logged but don't fail session start. The plugin degrades
    gracefully: tools return error JSON, prompt hook injects a "starting"
    placeholder, connector retries on each tick.
    """
    config = _state["config"]
    if config is None:
        logger.warning("nurture: on_session_start fired before register(ctx) — skipping")
        return
    if _state["started"]:
        return  # idempotent
    try:
        start_python_service(config)
    except Exception as e:
        logger.error("nurture: failed to start Python service: %s", e)

    # Start community connector (background thread) if community.url configured
    connector = _state.get("connector")
    if connector is not None:
        try:
            connector.start()
        except Exception as e:
            logger.error("nurture: failed to start community connector: %s", e)
    _state["started"] = True


def _on_session_end(**_kwargs: Any) -> None:
    """Hermes can spawn many sessions in one process — don't kill the
    Python service here. atexit handles process-level cleanup.
    """
    return


def register(ctx) -> None:
    """Plugin entry point — discovered by hermes ``PluginManager``.

    Wires:
      - 2 tools: nurture_execute, nurture_task_report
      - 3 hooks: on_session_start (spawn), on_session_end, pre_llm_call
      - 1 CLI subcommand: hermes nurture (status/list/migrate) — note
        upstream hermes argparse-wiring gap (audit 缺口 0); slash command
        ``/nurture`` is the working in-session path.
      - 1 slash command: /nurture
    """
    config = NurtureConfig.load()
    if not config.enabled:
        logger.info("nurture: plugin disabled via config")
        return

    _ensure_state(config)
    bridge: DeviceBridge = _state["bridge"]
    cap_provider: CachedCapabilityProvider = _state["cap_provider"]
    persona_provider: CachedPersonaProvider = _state["persona_provider"]

    # Community connector (Phase 3). Created here, started in on_session_start.
    connector: Optional[CommunityConnector] = None
    if config.community.url:
        connector = CommunityConnector(config, bridge)
        _state["connector"] = connector

    logger.info(
        "nurture plugin loaded (workspace=%s, device=%s, server=%s:%d, community=%s)",
        config.workspace_path,
        config.device_id or "auto",
        config.server_host,
        config.server_port,
        config.community.url or "(offline)",
    )

    # -- tools --------------------------------------------------------------

    bound_device = config.device_id or None
    ctx.register_tool(
        name="nurture_execute",
        toolset="nurture",
        schema=NURTURE_EXECUTE_SCHEMA,
        handler=make_nurture_execute_handler(bridge, bound_device),
    )

    # Phase 3: forward task outcomes to /api/task/complete via the connector.
    forward_fn = connector.forward_task_outcome if connector is not None else None
    ctx.register_tool(
        name="nurture_task_report",
        toolset="nurture",
        schema=NURTURE_TASK_REPORT_SCHEMA,
        handler=make_nurture_task_report_handler(forward_fn=forward_fn),
    )

    # -- hooks --------------------------------------------------------------

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook(
        "pre_llm_call",
        make_pre_llm_call_hook(cap_provider, persona_provider),
    )

    # -- CLI + slash command -----------------------------------------------

    ctx.register_cli_command(
        name="nurture",
        help="Avatar-Hermes device automation (status, list, migrate)",
        setup_fn=_register_nurture_cli,
        handler_fn=_nurture_command,
        description=(
            "Device automation engine that learns RPA recipes and shares them "
            "with a community of Hermes agents. See docs/avatar-hermes/."
            " NOTE: hermes upstream does not wire general-plugin CLI commands "
            "to argparse; use `/nurture` slash command in-session instead "
            "until that PR lands."
        ),
    )

    # Slash command — workaround for upstream hermes argparse-wiring gap
    # (docs/avatar-hermes/plugin-api-audit.md 缺口 0). Bind the bridge so
    # /nurture status / /nurture list can hit the live device service.
    ctx.register_command(
        name="nurture",
        handler=lambda raw_args: _slash_handler(raw_args, bridge=bridge, config=config),
        description="Avatar-Hermes device automation status / list",
        args_hint="<status|list>",
    )
