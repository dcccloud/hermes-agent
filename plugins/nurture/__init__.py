"""nurture plugin — Avatar-Hermes device automation.

Phase 0 scaffolding: registers a CLI subcommand and lifecycle hooks. Real
device service / community connector wiring lands in Phase 1+.

Design contracts: see ``docs/avatar-hermes/`` (README, decisions, boundary-
contracts, mcp-http-protocol).
"""
from __future__ import annotations

import logging

from .cli import register_cli as _register_nurture_cli
from .cli import nurture_command as _nurture_command
from .cli import slash_handler as _slash_handler

logger = logging.getLogger(__name__)


def _on_session_start(**kwargs) -> None:
    """Phase 0: log only. Phase 1 will spawn the Python device service here."""
    logger.debug("nurture: on_session_start (Phase 0 stub)")


def _on_session_end(**kwargs) -> None:
    """Phase 0: log only. Phase 1 will tear down the Python device service."""
    logger.debug("nurture: on_session_end (Phase 0 stub)")


def _pre_llm_call(**kwargs) -> None:
    """Phase 0: log only. Phase 1 will inject persona/goals/capabilities."""
    return None


def register(ctx) -> None:
    """Plugin entry point — discovered by hermes ``PluginManager``.

    Phase 0 only registers the CLI subcommand and lifecycle stubs so the
    plugin loads cleanly. Tool registration, MCP client setup, and the
    background sync thread land in Phase 1-3.
    """
    logger.info("nurture plugin loaded (Phase 0 scaffolding)")

    ctx.register_cli_command(
        name="nurture",
        help="Avatar-Hermes device automation (status, list, migrate)",
        setup_fn=_register_nurture_cli,
        handler_fn=_nurture_command,
        description=(
            "Device automation engine that learns RPA recipes and shares them "
            "with a community of Hermes agents. See "
            "docs/avatar-hermes/README.md for the full design. "
            "NOTE: hermes upstream does not yet wire general-plugin CLI "
            "commands to argparse; use `/nurture` slash command in-session "
            "instead until that PR lands."
        ),
    )

    # Slash command — works around the upstream hermes argparse-wiring gap.
    # See docs/avatar-hermes/plugin-api-audit.md "缺口 0".
    ctx.register_command(
        name="nurture",
        handler=_slash_handler,
        description="Avatar-Hermes device automation status / list",
        args_hint="<status|list>",
    )

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
