"""CLI commands for the nurture plugin.

Wires ``hermes nurture <subcommand>`` (CLI argparse) and ``/nurture
<subcommand>`` (in-session slash command, the working path because
upstream hermes does not wire general-plugin CLI commands — see
``docs/avatar-hermes/plugin-api-audit.md`` 缺口 0).

Subcommands:
  status      — plugin / Python service / community state
  list        — locally learned recipes (Phase 1+ uses live bridge)
  migrate     — OpenClaw → Hermes data migration (Phase 6)
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes nurture`` argparse tree."""
    subs = subparser.add_subparsers(dest="nurture_command")

    subs.add_parser("status", help="Show device service / community connection status")

    list_p = subs.add_parser("list", help="List learned recipes")
    list_p.add_argument("--app", default=None, help="Filter by app (e.g. 'douyin')")
    list_p.add_argument("--json", action="store_true", help="Output JSON")

    migrate_p = subs.add_parser(
        "migrate",
        help="Migrate device data from OpenClaw to Hermes (Phase 6)",
    )
    migrate_p.add_argument("--source", default="~/.openclaw")
    migrate_p.add_argument("--dry-run", action="store_true")
    migrate_p.add_argument("--overwrite", action="store_true")
    migrate_p.add_argument("--skip-recipes", action="store_true")
    migrate_p.add_argument("--app", default=None, help="Migrate only one app")


def nurture_command(args: argparse.Namespace) -> int:
    """Dispatch ``hermes nurture <subcommand>`` (when argparse gets fixed)."""
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
    bridge, config = _build_standalone_bridge()
    print(_render_status(bridge, config))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    bridge, _ = _build_standalone_bridge()
    if bridge is None:
        print("nurture list: device service unreachable (is the plugin running?)")
        return 1
    try:
        caps = bridge.get_capabilities()
    except Exception as e:
        print(f"nurture list: failed to fetch capabilities: {e}")
        return 1

    if getattr(args, "json", False):
        print(json.dumps(caps, indent=2, ensure_ascii=False))
        return 0

    apps = caps.get("apps") or {}
    if args.app:
        apps = {args.app: apps[args.app]} if args.app in apps else {}
        if not apps:
            print(f"nurture list: app {args.app!r} not found")
            return 1

    for app_name, app_data in apps.items():
        ops = app_data.get("operations") or []
        print(f"\n{app_name}: {len(ops)} operations")
        for op in ops:
            recipe = "✓" if op.get("has_recipe") else "✗"
            print(f"  [{recipe}] {op.get('id'):40s} {op.get('display_name', '')}")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    print("nurture migrate: not yet implemented (lands in Phase 6)")
    print(f"  source:    {args.source}")
    print(f"  dry-run:   {args.dry_run}")
    return 1


# ---------------------------------------------------------------------------
# Slash command handler (in-session "/nurture <subcommand>")
# ---------------------------------------------------------------------------

def slash_handler(raw_args: str, *, bridge: Any = None, config: Any = None) -> str:
    """Slash command handler invoked as ``/nurture <subcommand>`` in a hermes
    session. The bridge / config are kwargs bound at registration time.

    Returns plain text to display.
    """
    parts = (raw_args or "").strip().split()
    sub = parts[0] if parts else "status"

    if sub == "status":
        return _render_status(bridge, config)

    if sub == "list":
        if bridge is None:
            return "nurture list: device service unreachable"
        try:
            caps = bridge.get_capabilities()
        except Exception as e:
            return f"nurture list: failed to fetch capabilities: {e}"
        apps = caps.get("apps") or {}
        if not apps:
            return "nurture list: no apps configured"
        lines = []
        for app_name, app_data in apps.items():
            ops = app_data.get("operations") or []
            lines.append(f"{app_name}: {len(ops)} operations")
            for op in ops[:10]:
                recipe = "✓" if op.get("has_recipe") else "✗"
                lines.append(f"  [{recipe}] {op.get('id', '?')}")
            if len(ops) > 10:
                lines.append(f"  ... and {len(ops) - 10} more")
        return "\n".join(lines)

    if sub == "migrate":
        return "nurture migrate: not yet implemented (lands in Phase 6)"

    return f"/nurture: unknown subcommand {sub!r}. Try: status, list, migrate"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_standalone_bridge():
    """Build a fresh DeviceBridge from config — for CLI invocations that
    don't share plugin state with a running session.
    """
    try:
        from .config import NurtureConfig
        from .device_bridge import DeviceBridge

        config = NurtureConfig.load()
        if not config.enabled:
            return None, config
        return DeviceBridge(config.server_host, config.server_port), config
    except Exception as e:
        logger.debug("nurture: failed to build standalone bridge: %s", e)
        return None, None


def _render_status(bridge: Optional[Any], config: Optional[Any]) -> str:
    """Render plugin / Python service / community status as plain text."""
    lines = ["nurture status:"]
    if config is None:
        lines.append("  config:          (not loaded)")
        return "\n".join(lines)

    lines.append(f"  enabled:         {config.enabled}")
    lines.append(f"  agent id:        {config.agent_id or '(auto)'}")
    lines.append(f"  device id:       {config.device_id or '(auto-select first online)'}")
    lines.append(f"  workspace:       {config.workspace_path}")
    lines.append(f"  server:          {config.server_host}:{config.server_port}")

    if bridge is None:
        lines.append("  Python service:  (no bridge — config disabled or import failed)")
        return "\n".join(lines)

    try:
        health = bridge.health()
        env = health.get("environment", "?")
        lines.append(f"  Python service:  ✓ healthy (env={env})")
    except Exception as e:
        lines.append(f"  Python service:  ✗ unreachable ({e})")
        lines.append("                   (Phase 1: did you `hermes plugins enable nurture`?)")

    if config.community.url:
        lines.append(f"  community URL:   {config.community.url}")
        lines.append("  community:       (Phase 3 — MCP + polling not yet wired)")
    else:
        lines.append("  community:       offline (no URL configured)")

    return "\n".join(lines)
