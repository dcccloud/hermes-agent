"""nurture-community plugin — Avatar-Hermes community knowledge engine.

Phase 0 scaffolding. Real FastAPI / MCP server / 7-store knowledge engine
land in Phase 2-3.

Design contracts: see ``docs/avatar-hermes/``.
"""
from __future__ import annotations

import logging

from .cli import register_cli as _register_cli
from .cli import community_command as _community_command
from .cli import slash_handler as _slash_handler

logger = logging.getLogger(__name__)


def _on_session_start(**kwargs) -> None:
    """Phase 0: log only. Phase 2 will start FastAPI :18790 + KnowledgeEngine."""
    logger.debug("nurture-community: on_session_start (Phase 0 stub)")


def _on_session_end(**kwargs) -> None:
    """Phase 0: log only. Phase 2 will gracefully stop the server."""
    logger.debug("nurture-community: on_session_end (Phase 0 stub)")


def register(ctx) -> None:
    """Plugin entry point — discovered by hermes ``PluginManager``.

    Phase 0 only registers the CLI subcommand and lifecycle stubs.
    """
    logger.info("nurture-community plugin loaded (Phase 0 scaffolding)")

    ctx.register_cli_command(
        name="nurture-community",
        help="Avatar-Hermes community knowledge engine (status, ...)",
        setup_fn=_register_cli,
        handler_fn=_community_command,
        description=(
            "Run a community-side hermes instance that aggregates recipes / "
            "graphs / traces / events from many devices and dispatches tasks. "
            "See docs/avatar-hermes/community-agent.md. "
            "NOTE: use `/nurture-community` slash command in-session until "
            "hermes upstream wires general-plugin CLI commands to argparse."
        ),
    )

    # Slash command — workaround for upstream hermes argparse-wiring gap.
    ctx.register_command(
        name="nurture-community",
        handler=_slash_handler,
        description="Avatar-Hermes community knowledge engine status",
        args_hint="<status|agents|recipes|tasks>",
    )

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
