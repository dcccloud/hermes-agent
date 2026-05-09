"""CLI commands for the nurture-community plugin.

Wires ``hermes nurture-community <subcommand>``:
  status      — print FastAPI / DB / connected agents state
  agents      — list registered agents
  recipes     — list / show / fuse recipes
  tasks       — create / list / cancel community tasks (admin)
  serve       — start FastAPI + MCP server in foreground (Phase 2 dev mode)

Phase 0 only stubs subcommand parsing. Implementations land in Phase 2-3.
"""
from __future__ import annotations

import argparse
import logging

logger = logging.getLogger(__name__)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes nurture-community`` argparse tree."""
    subs = subparser.add_subparsers(dest="community_command")

    subs.add_parser("status", help="Show community server / DB / agents status")

    agents_p = subs.add_parser("agents", help="List registered agents")
    agents_p.add_argument("--json", action="store_true")

    recipes_p = subs.add_parser("recipes", help="Recipe registry tools")
    recipes_subs = recipes_p.add_subparsers(dest="recipes_subcommand")
    recipes_subs.add_parser("list")
    recipes_subs.add_parser("fuse", help="Force-trigger recipe fusion (Phase 2)")

    tasks_p = subs.add_parser("tasks", help="Community task management (admin)")
    tasks_subs = tasks_p.add_subparsers(dest="tasks_subcommand")
    tasks_subs.add_parser("list")
    create_p = tasks_subs.add_parser("create")
    create_p.add_argument("--app", required=True)
    create_p.add_argument("--description", required=True)
    create_p.add_argument("--priority", default="normal", choices=("normal", "high"))

    serve_p = subs.add_parser(
        "serve",
        help="Run the community FastAPI + MCP server in the foreground",
    )
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=18790)
    serve_p.add_argument(
        "--workspace", default=None,
        help="Workspace dir (defaults to $HERMES_HOME/nurture-community/)",
    )
    serve_p.add_argument(
        "--database-url", default=None,
        help="PostgreSQL DSN (omit for JSON file backend)",
    )

    analyze_p = subs.add_parser(
        "analyze",
        help="Aggregate trace data and generate advice (Phase 5)",
    )
    analyze_p.add_argument(
        "--days", type=int, default=7,
        help="Window size in days (default 7)",
    )
    analyze_p.add_argument(
        "--app", default=None,
        help="Aggregate only one app (default: all tracked apps)",
    )
    analyze_p.add_argument("--json", action="store_true", help="Output JSON")

    report_p = subs.add_parser(
        "report",
        help="Build weekly markdown report (designed for cron + send_message)",
    )
    report_p.add_argument(
        "--days", type=int, default=7,
        help="Window size in days (default 7)",
    )


def community_command(args: argparse.Namespace) -> int:
    """Dispatch ``hermes nurture-community <subcommand>``."""
    cmd = getattr(args, "community_command", None)
    if cmd is None:
        print("hermes nurture-community: missing subcommand. Try `--help`.")
        return 2

    if cmd == "status":
        return _cmd_status(args)
    if cmd == "agents":
        print("nurture-community agents: not yet implemented (Phase 2)")
        return 1
    if cmd == "recipes":
        print("nurture-community recipes: not yet implemented (Phase 2)")
        return 1
    if cmd == "tasks":
        print("nurture-community tasks: not yet implemented (Phase 2-3)")
        return 1
    if cmd == "serve":
        return _cmd_serve(args)
    if cmd == "analyze":
        return _cmd_analyze(args)
    if cmd == "report":
        return _cmd_report(args)

    print(f"hermes nurture-community: unknown subcommand {cmd!r}")
    return 2


def _cmd_status(args: argparse.Namespace) -> int:
    print("nurture-community status:")
    print("  plugin loaded:   yes (Phase 0 scaffolding)")
    print("  FastAPI :18790:  not yet started (Phase 2)")
    print("  KnowledgeEngine: not yet initialised (Phase 2)")
    print("  MCP server:      not yet exposed (Phase 3)")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the community FastAPI + MCP server in the foreground.

    Bootstraps the same path the plugin's on_session_start hook uses,
    then blocks on SIGINT/SIGTERM. Use this from shell scripts /
    systemd / launchd / Docker without needing hermes session start.
    """
    import asyncio
    import logging
    import os
    import signal
    import threading

    from .auth import JwtAuth
    from .avatar_registry import AvatarRegistry
    from .community_server import create_app
    from .knowledge_engine import KnowledgeEngine

    logging.basicConfig(
        level=os.environ.get("NURTURE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    log = logging.getLogger("nurture-community.serve")

    # Workspace + DB resolution mirrors plugins/nurture-community/__init__.py
    from hermes_constants import get_hermes_home

    workspace = (
        args.workspace
        and __import__("pathlib").Path(args.workspace).expanduser().resolve()
    ) or os.environ.get("NURTURE_COMMUNITY_WORKSPACE")
    if not workspace:
        workspace = get_hermes_home() / "nurture-community"
    else:
        from pathlib import Path
        workspace = Path(workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "knowledge").mkdir(parents=True, exist_ok=True)
    (workspace / "secrets").mkdir(parents=True, exist_ok=True)

    db_url = (
        args.database_url
        or os.environ.get("NURTURE_COMMUNITY_DATABASE_URL")
        or None
    )

    engine = KnowledgeEngine(database_url=db_url, store_dir=workspace / "knowledge")
    auth = JwtAuth(workspace / "secrets")
    registry = AvatarRegistry()

    log.info(
        "nurture-community serve: workspace=%s db=%s",
        workspace, "PG" if db_url else "JSON",
    )

    async def main() -> None:
        await engine.init()
        app = create_app(
            knowledge_engine=engine, auth=auth, avatar_registry=registry,
        )
        # Optional: mount MCP under /mcp
        try:
            from .mcp_server import create_mcp_server
            mcp = create_mcp_server(engine)
            app.mount("/mcp", mcp.streamable_http_app())
            log.info("MCP server mounted at /mcp")
        except Exception as e:
            log.warning("MCP server unavailable: %s — REST endpoints still work", e)

        import uvicorn
        config = uvicorn.Config(
            app=app,
            host=args.host, port=args.port,
            log_level=os.environ.get("NURTURE_LOG_LEVEL", "info").lower(),
        )
        server = uvicorn.Server(config)

        # Graceful shutdown on SIGTERM/SIGINT
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig, lambda: setattr(server, "should_exit", True)
                )
            except (NotImplementedError, AttributeError):
                pass

        log.info("nurture-community serve: listening on %s:%d", args.host, args.port)
        await server.serve()
        await engine.close()

    asyncio.run(main())
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    """Aggregate trace data + run advice rules over the window."""
    import asyncio
    import json as _json
    from datetime import datetime, timedelta, timezone

    engine = _build_standalone_engine()
    if engine is None:
        print("nurture-community analyze: cannot reach KnowledgeEngine")
        return 1

    async def run() -> int:
        await engine.init()
        from .analytics import aggregate_all, aggregate_app_metrics
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=args.days)
        if args.app:
            metrics = await aggregate_app_metrics(
                engine, args.app, since=since, until=now,
            )
            payload = {args.app: metrics}
        else:
            report = await aggregate_all(engine, since=since, until=now)
            payload = report.to_dict()

        if args.json:
            print(_json.dumps(payload, indent=2, default=_json_default, ensure_ascii=False))
        else:
            print(f"window: {args.days}d")
            if args.app:
                print(f"  app:           {args.app}")
                print(f"  total_traces:  {metrics.total_traces}")
                print(f"  success_rate:  {metrics.success_rate * 100:.1f}%")
                print(f"  ops:           {len(metrics.operations)}")
            else:
                apps = payload.get("apps") or {}
                for name, m in sorted(apps.items()):
                    print(
                        f"  {name:14s} traces={m['total_traces']:6d} "
                        f"success={m['success_rate'] * 100:5.1f}% "
                        f"agents={m['distinct_agents']}"
                    )

        # Run advice rules across all apps so the next /api/advices/poll
        # reflects fresh analysis.
        apps_to_analyze = [args.app] if args.app else list(payload.get("apps", {}).keys())
        for a in apps_to_analyze:
            generated = await engine.analyze_and_generate_advice(a)
            if generated:
                print(f"  ✓ generated {generated} advice entries for {a}")
        return 0

    try:
        return asyncio.run(run())
    finally:
        try:
            asyncio.run(engine.close())
        except Exception:
            pass


def _cmd_report(args: argparse.Namespace) -> int:
    """Print a markdown report — pipe to a cron deliver target."""
    import asyncio

    engine = _build_standalone_engine()
    if engine is None:
        print("[SILENT] nurture-community: cannot reach KnowledgeEngine")
        return 1

    async def run() -> int:
        await engine.init()
        from .analytics import build_weekly_report
        text = await build_weekly_report(engine)
        print(text)
        return 0

    try:
        return asyncio.run(run())
    finally:
        try:
            asyncio.run(engine.close())
        except Exception:
            pass


def _json_default(obj: object) -> object:
    """JSON encoder fallback for dataclass instances (AppMetrics, etc.)."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    raise TypeError(f"not serializable: {type(obj).__name__}")


def _build_standalone_engine():
    """Build a KnowledgeEngine for ad-hoc CLI use.

    Mirrors the wiring in __init__.py but doesn't start the FastAPI
    server — just opens stores and runs migrations.
    """
    try:
        from hermes_cli.config import cfg_get, load_config
        from hermes_constants import get_hermes_home

        from .knowledge_engine import KnowledgeEngine
    except ImportError:
        return None

    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    section = cfg_get(cfg, "plugins", "nurture-community", default={}) or {}
    db_url = section.get("databaseUrl") or None
    workspace = section.get("workspace")
    import os
    if not workspace:
        workspace = os.environ.get("NURTURE_COMMUNITY_WORKSPACE")
    if workspace:
        from pathlib import Path
        store_dir = Path(workspace).expanduser().resolve() / "knowledge"
    else:
        store_dir = get_hermes_home() / "nurture-community" / "knowledge"
    store_dir.mkdir(parents=True, exist_ok=True)
    return KnowledgeEngine(database_url=db_url, store_dir=store_dir)


# ---------------------------------------------------------------------------
# Slash command handler (in-session "/nurture-community status")
# ---------------------------------------------------------------------------

def slash_handler(raw_args: str) -> str:
    """Slash command handler — invoked as ``/nurture-community <subcommand>``.

    Workaround for upstream hermes argparse-wiring gap.
    """
    parts = (raw_args or "").strip().split()
    sub = parts[0] if parts else "status"

    if sub == "status":
        return (
            "nurture-community status:\n"
            "  plugin loaded:   yes (Phase 0 scaffolding)\n"
            "  FastAPI :18790:  not yet started (Phase 2)\n"
            "  KnowledgeEngine: not yet initialised (Phase 2)\n"
            "  MCP server:      not yet exposed (Phase 3)"
        )
    if sub == "agents":
        return "nurture-community agents: not yet implemented (Phase 2)"
    if sub == "recipes":
        return "nurture-community recipes: not yet implemented (Phase 2)"
    if sub == "tasks":
        return "nurture-community tasks: not yet implemented (Phase 2-3)"
    return f"/nurture-community: unknown subcommand {sub!r}"
