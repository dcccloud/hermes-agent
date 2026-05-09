"""nurture-community plugin — Avatar-Hermes community knowledge engine.

Phase 3: spawns a uvicorn FastAPI server (with MCP endpoint mounted at
``/mcp``) on session start. Server runs in a background thread keyed
to the hermes process lifetime; ``atexit`` handles cleanup.

Design contracts: see ``docs/avatar-hermes/``.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_cli.config import cfg_get, load_config
from hermes_constants import get_hermes_home

from .cli import community_command as _community_command
from .cli import register_cli as _register_cli
from .cli import slash_handler as _slash_handler

logger = logging.getLogger(__name__)


# Process-level singletons set on first session start.
_state: Dict[str, Any] = {
    "engine": None,
    "auth": None,
    "registry": None,
    "server_thread": None,
    "uvicorn_server": None,
    "atexit_registered": False,
}


def _config() -> Dict[str, Any]:
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    return cfg_get(cfg, "plugins", "nurture-community", default={}) or {}


def _load_database_url() -> str:
    """Load DB URL from plugin config or env."""
    section = _config()
    return (
        section.get("databaseUrl")
        or os.environ.get("NURTURE_COMMUNITY_DATABASE_URL")
        or ""
    )


def _resolve_workspace() -> Path:
    """Community state root. Defaults to ``$HERMES_HOME/nurture-community``."""
    section = _config()
    raw = section.get("workspace") or os.environ.get("NURTURE_COMMUNITY_WORKSPACE")
    if raw:
        return Path(raw).expanduser().resolve()
    return get_hermes_home() / "nurture-community"


def _start_server_thread(host: str, port: int) -> None:
    """Spawn the FastAPI + MCP server in a background thread.

    Idempotent (no-op if already running).
    """
    if _state["server_thread"] is not None and _state["server_thread"].is_alive():
        return

    workspace = _resolve_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    knowledge_dir = workspace / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    secrets_dir = workspace / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)

    # Lazy import — heavy deps (fastapi, uvicorn, mcp) only load when
    # the plugin is actually enabled.
    from .auth import JwtAuth
    from .avatar_registry import AvatarRegistry
    from .community_server import create_app
    from .knowledge_engine import KnowledgeEngine
    from .mcp_server import create_mcp_server

    engine = KnowledgeEngine(
        database_url=_load_database_url() or None,
        store_dir=knowledge_dir,
    )
    auth = JwtAuth(secrets_dir)
    registry = AvatarRegistry()

    _state["engine"] = engine
    _state["auth"] = auth
    _state["registry"] = registry

    def run_server() -> None:
        import uvicorn

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Run engine.init() (PG migration, no-op for JSON)
        try:
            loop.run_until_complete(engine.init())
        except Exception:
            logger.exception("nurture-community: engine.init() failed")
            return

        app = create_app(
            knowledge_engine=engine, auth=auth, avatar_registry=registry,
        )

        # Mount MCP server under /mcp via Streamable HTTP transport.
        try:
            mcp = create_mcp_server(engine)
            app.mount("/mcp", mcp.streamable_http_app())
            logger.info("nurture-community: MCP server mounted at /mcp")
        except Exception as e:
            logger.warning(
                "nurture-community: MCP server unavailable (%s); REST endpoints still work",
                e,
            )

        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="info",
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        _state["uvicorn_server"] = server
        try:
            loop.run_until_complete(server.serve())
        except Exception:
            logger.exception("nurture-community: server crashed")
        finally:
            try:
                loop.run_until_complete(engine.close())
            except Exception:
                pass

    thread = threading.Thread(
        target=run_server,
        name="nurture-community-server",
        daemon=True,
    )
    thread.start()
    _state["server_thread"] = thread

    if not _state["atexit_registered"]:
        atexit.register(_stop_server_thread)
        _state["atexit_registered"] = True

    logger.info(
        "nurture-community: FastAPI server starting on %s:%d (workspace=%s, db=%s)",
        host, port, workspace,
        "PG" if _load_database_url() else "JSON",
    )


def _stop_server_thread() -> None:
    server = _state.get("uvicorn_server")
    if server is not None:
        server.should_exit = True
    thread = _state.get("server_thread")
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)


def _on_session_start(**_kwargs: Any) -> None:
    section = _config()
    if not section.get("enabled", False):
        logger.debug("nurture-community: plugin disabled via config")
        return
    host = section.get("host", "127.0.0.1")
    port = int(section.get("port", 18790) or 18790)
    try:
        _start_server_thread(host, port)
    except Exception:
        logger.exception("nurture-community: failed to start server")


def _on_session_end(**_kwargs: Any) -> None:
    # Long-lived server; let atexit handle final cleanup so multiple
    # sessions in one hermes process share the same backend.
    return


def register(ctx) -> None:
    """Plugin entry point — discovered by hermes ``PluginManager``."""
    logger.info("nurture-community plugin loaded (Phase 3)")

    ctx.register_cli_command(
        name="nurture-community",
        help="Avatar-Hermes community knowledge engine (status, agents, recipes, tasks)",
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

    ctx.register_command(
        name="nurture-community",
        handler=_slash_handler,
        description="Avatar-Hermes community knowledge engine status",
        args_hint="<status|agents|recipes|tasks>",
    )

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
