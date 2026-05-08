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

    serve_p = subs.add_parser("serve", help="Start FastAPI + MCP server (Phase 2 dev)")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=18790)


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
        print("nurture-community serve: not yet implemented (Phase 2)")
        return 1

    print(f"hermes nurture-community: unknown subcommand {cmd!r}")
    return 2


def _cmd_status(args: argparse.Namespace) -> int:
    print("nurture-community status:")
    print("  plugin loaded:   yes (Phase 0 scaffolding)")
    print("  FastAPI :18790:  not yet started (Phase 2)")
    print("  KnowledgeEngine: not yet initialised (Phase 2)")
    print("  MCP server:      not yet exposed (Phase 3)")
    return 0


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
