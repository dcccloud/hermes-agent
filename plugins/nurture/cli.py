"""CLI commands for the nurture plugin.

Wires ``hermes nurture <subcommand>``:
  status      — print plugin / device service / community connection state
  list        — list locally learned recipes
  migrate     — migrate from OpenClaw (~/.openclaw/...) — Phase 6

Phase 0: subcommands print "not yet implemented" placeholders. Real
implementations land in Phase 1 (status/list) and Phase 6 (migrate).
"""
from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes nurture`` argparse tree.

    Called once at plugin load time by hermes ``PluginManager``.
    """
    subs = subparser.add_subparsers(dest="nurture_command")

    subs.add_parser("status", help="Show device service / community connection status")

    list_p = subs.add_parser("list", help="List learned recipes")
    list_p.add_argument("--app", default=None, help="Filter by app (e.g. 'douyin')")
    list_p.add_argument("--json", action="store_true", help="Output JSON")

    migrate_p = subs.add_parser(
        "migrate",
        help="Migrate device data from OpenClaw to Hermes (Phase 6)",
    )
    migrate_p.add_argument(
        "--source", default="~/.openclaw",
        help="OpenClaw home directory (default: ~/.openclaw)",
    )
    migrate_p.add_argument("--dry-run", action="store_true")
    migrate_p.add_argument("--overwrite", action="store_true")
    migrate_p.add_argument("--skip-recipes", action="store_true")
    migrate_p.add_argument("--app", default=None, help="Migrate only one app")


def nurture_command(args: argparse.Namespace) -> int:
    """Dispatch ``hermes nurture <subcommand>``.

    Returns the process exit code.
    """
    cmd = getattr(args, "nurture_command", None)
    if cmd is None:
        print("hermes nurture: missing subcommand. Try `hermes nurture --help`.")
        return 2

    if cmd == "status":
        return _cmd_status(args)
    if cmd == "list":
        return _cmd_list(args)
    if cmd == "migrate":
        return _cmd_migrate(args)

    print(f"hermes nurture: unknown subcommand {cmd!r}")
    return 2


def _cmd_status(args: argparse.Namespace) -> int:
    print("nurture status:")
    print("  plugin loaded:   yes (Phase 0 scaffolding)")
    print("  device service:  not yet wired (Phase 1)")
    print("  community:       not yet wired (Phase 3)")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    print("nurture list: not yet implemented (lands in Phase 1)")
    return 1


def _cmd_migrate(args: argparse.Namespace) -> int:
    print("nurture migrate: not yet implemented (lands in Phase 6)")
    print(f"  source:    {args.source}")
    print(f"  dry-run:   {args.dry_run}")
    return 1


# ---------------------------------------------------------------------------
# Slash command handler (in-session "/nurture status")
# ---------------------------------------------------------------------------

def slash_handler(raw_args: str) -> str:
    """Slash command handler — invoked as ``/nurture <subcommand>`` in a hermes
    session. Returns text to display to the user.

    Workaround for upstream hermes not wiring general-plugin CLI commands to
    argparse. See docs/avatar-hermes/plugin-api-audit.md "缺口 0".
    """
    parts = (raw_args or "").strip().split()
    sub = parts[0] if parts else "status"

    if sub == "status":
        return (
            "nurture status:\n"
            "  plugin loaded:   yes (Phase 0 scaffolding)\n"
            "  device service:  not yet wired (Phase 1)\n"
            "  community:       not yet wired (Phase 3)"
        )
    if sub == "list":
        return "nurture list: not yet implemented (lands in Phase 1)"
    if sub == "migrate":
        return "nurture migrate: not yet implemented (lands in Phase 6)"
    return f"/nurture: unknown subcommand {sub!r}. Try: status, list, migrate"
