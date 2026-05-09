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

    serve_p = subs.add_parser(
        "serve",
        help="Run the device-side nurture stack in the foreground "
             "(Python device service + community connector). Use this "
             "from shell scripts / systemd / launchd / Docker.",
    )
    serve_p.add_argument(
        "--no-community", action="store_true",
        help="Skip community connector even if community.url is configured",
    )

    init_w = subs.add_parser(
        "init-worker",
        help="Pre-set the nurture-task-worker profile so the kanban "
             "dispatcher can spawn workers (Phase 4)",
    )
    init_w.add_argument(
        "--profile", default="nurture-task-worker",
        help="Profile name to create / update (default: nurture-task-worker)",
    )
    init_w.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing profile",
    )


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
    if cmd == "init-worker":
        return _cmd_init_worker(args)
    if cmd == "serve":
        return _cmd_serve(args)
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
    """OpenClaw → Avatar-Hermes data migration. Pure file copy."""
    from .config import NurtureConfig
    from .migrate import build_plan, execute_plan, expand_user, validate_migration

    source = expand_user(args.source)
    if not source.exists():
        print(f"nurture migrate: source {source} does not exist")
        return 1

    config = NurtureConfig.load()
    dest = config.workspace_path
    dest.mkdir(parents=True, exist_ok=True)

    plan = build_plan(
        source, dest,
        skip_recipes=getattr(args, "skip_recipes", False),
        only_app=getattr(args, "app", None),
    )

    print(f"nurture migrate")
    print(f"  source:        {source}")
    print(f"  dest:          {dest}")
    print(f"  items planned: {plan.total_items()}")
    if plan.skipped_apps:
        print(f"  skipped apps:  {', '.join(plan.skipped_apps)}")

    if not plan.mappings:
        print()
        print("Nothing to migrate.")
        return 0

    if plan.conflicts and not args.overwrite:
        print()
        print(f"  ⚠ {len(plan.conflicts)} item(s) already exist at the destination:")
        for c in plan.conflicts[:5]:
            print(f"    {c}")
        if len(plan.conflicts) > 5:
            print(f"    ... and {len(plan.conflicts) - 5} more")
        print()
        print("Pass --overwrite to replace, or --dry-run to inspect first.")
        return 1

    if args.dry_run:
        print()
        print("Dry run — would copy:")
        # Group by category for compact output
        from collections import defaultdict
        groups = defaultdict(list)
        for m in plan.mappings:
            groups[m.category].append(m)
        for category, mappings in sorted(groups.items()):
            print(f"  {category}: {len(mappings)} item(s)")
            for m in mappings[:3]:
                print(f"    {m.source.name}")
            if len(mappings) > 3:
                print(f"    ... and {len(mappings) - 3} more")
        return 0

    result = execute_plan(plan, overwrite=args.overwrite)
    print()
    print(f"  copied:        {result.items_copied}")
    print(f"  skipped:       {result.items_skipped}")
    print(f"  failed:        {result.items_failed}")
    print(f"  bytes:         {result.bytes_copied:,}")

    if result.failures:
        print()
        print("Failures:")
        for f in result.failures[:10]:
            print(f"  {f}")
        return 1

    # Validate
    warnings = validate_migration(dest)
    if warnings:
        print()
        print("⚠ Validation warnings (non-fatal):")
        for w in warnings[:10]:
            print(f"  {w}")
        if len(warnings) > 10:
            print(f"  ... and {len(warnings) - 10} more")
    else:
        print()
        print("✓ Validation passed.")

    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the device-side stack (Python service + connector) in foreground.

    Mirrors plugins/nurture/__init__.py register() / on_session_start
    wiring but blocks on SIGINT/SIGTERM. Lets the user run the device
    side without an interactive hermes REPL or messaging gateway.
    """
    import logging
    import os
    import signal
    import threading

    from .community_connector import CommunityConnector
    from .config import NurtureConfig
    from .device_bridge import DeviceBridge
    from .service import start_python_service, stop_python_service

    logging.basicConfig(
        level=os.environ.get("NURTURE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    log = logging.getLogger("nurture.serve")

    config = NurtureConfig.load()
    if not config.enabled:
        log.error("nurture serve: plugin disabled in config")
        return 1

    # 1. Spawn Python device service (idempotent)
    if not start_python_service(config):
        log.warning("nurture serve: Python service did not start; check logs")
        return 1

    # 2. Build bridge + (optionally) start community connector
    bridge = DeviceBridge(config.server_host, config.server_port)
    connector = None
    if config.community.url and not args.no_community:
        connector = CommunityConnector(config, bridge)
        connector.start()
        log.info("nurture serve: community connector started")
    else:
        log.info(
            "nurture serve: community connector skipped (url=%s, no_community=%s)",
            bool(config.community.url), args.no_community,
        )

    # 3. Block on SIGINT/SIGTERM
    log.info(
        "nurture serve: device service ready on %s:%d, "
        "device=%s, workspace=%s. Ctrl+C to stop.",
        config.server_host, config.server_port,
        config.device_id or "auto",
        config.workspace_path,
    )
    stop_event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop_event.set())
        except ValueError:
            pass

    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if connector is not None:
            connector.stop()
        stop_python_service()
        bridge.close()
        log.info("nurture serve: stopped")
    return 0


def _cmd_init_worker(args: argparse.Namespace) -> int:
    """Create / update the nurture-task-worker hermes profile.

    The profile is the target of kanban_create's ``assignee`` field:
    when the dispatcher sees a task assigned to ``nurture-task-worker``,
    it spawns a hermes process with that profile, which loads the
    nurture plugin and gets a Pi Agent ready to execute device ops.
    """
    profile = args.profile
    force = args.force
    try:
        from hermes_constants import get_hermes_home
    except ImportError as e:
        print(f"hermes_constants unavailable: {e}")
        return 1

    profile_dir = get_hermes_home().parent / ".hermes" / "profiles" / profile
    profile_dir = profile_dir.expanduser()
    if profile_dir.exists() and not force:
        print(f"nurture init-worker: profile {profile!r} already exists at {profile_dir}")
        print("  Pass --force to overwrite.")
        return 0

    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path = profile_dir / "config.yaml"
    config_path.write_text(_DEFAULT_WORKER_PROFILE_YAML, encoding="utf-8")
    print(f"nurture init-worker: profile {profile!r} ready at {profile_dir}")
    print(f"  config:  {config_path}")
    print()
    print("Next step: enable the plugin in this profile (or copy your")
    print("  main config's `plugins.enabled` list manually):")
    print(f"  hermes --profile {profile} plugins enable nurture")
    print()
    print("Then point your community plugin at this profile:")
    print(f"  hermes config set plugins.nurture.community.workerProfile {profile}")
    return 0


_DEFAULT_WORKER_PROFILE_YAML = """# nurture-task-worker hermes profile
#
# This profile is spawned by the kanban dispatcher when a community task
# arrives. The plugin must be enabled here (or via `hermes --profile
# nurture-task-worker plugins enable nurture`).
#
# delegation.subagent_auto_approve avoids prompting on every nurture_execute
# call — workers are non-interactive.

plugins:
  enabled:
    - nurture

delegation:
  subagent_auto_approve: true
"""


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
