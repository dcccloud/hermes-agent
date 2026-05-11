#!/usr/bin/env python3
"""Admin task management — create / list / show / complete / cleanup.

Wraps ``plugins.nurture_community.cli._cmd_tasks`` for direct shell use,
since the hermes upstream argparse doesn't wire general-plugin CLI
commands (see plugin-api-audit.md 缺口 0).

Usage::

    # List all tasks
    python plugins/nurture-community/scripts/tasks.py list

    # Publish a new task
    python plugins/nurture-community/scripts/tasks.py create \\
        --app douyin \\
        --description "在抖音首页点赞 5 个美食视频" \\
        --priority normal \\
        --require-cap douyin.give_a_like

    # Show one task
    python plugins/nurture-community/scripts/tasks.py show task-1762430400-1

    # Manually complete (admin override)
    python plugins/nurture-community/scripts/tasks.py complete task-1762430400-1

    # Mark expired + prune old ones
    python plugins/nurture-community/scripts/tasks.py cleanup

The script runs in-process against the JSON / PG backend, so when the
community server is running on the same machine they share state.
For a remote admin, use the corresponding REST endpoint (todo Phase 5).
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import types
from pathlib import Path


def _ensure_plugin_loaded() -> None:
    parent = "hermes_plugins"
    if parent not in sys.modules:
        ns = types.ModuleType(parent)
        ns.__path__ = []
        ns.__package__ = parent
        sys.modules[parent] = ns

    module_name = f"{parent}.nurture_community"
    if module_name in sys.modules:
        return

    plugin_dir = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        module_name, plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = module_name
    mod.__path__ = [str(plugin_dir)]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nurture-community-tasks",
        description="Avatar-Hermes community task management (admin).",
    )
    sub = parser.add_subparsers(dest="tasks_subcommand")

    list_t = sub.add_parser("list", help="List tasks")
    list_t.add_argument(
        "--status", default=None,
        choices=("active", "completed", "expired"),
    )
    list_t.add_argument("--app", default=None)
    list_t.add_argument("--json", action="store_true")

    create_p = sub.add_parser("create", help="Publish a new task")
    create_p.add_argument("--app", required=True)
    create_p.add_argument("--description", required=True)
    create_p.add_argument("--priority", default="normal", choices=("normal", "high"))
    create_p.add_argument("--expires", default=None)
    create_p.add_argument("--platform", action="append", default=None)
    create_p.add_argument("--require-cap", action="append", default=None)

    show_p = sub.add_parser("show", help="Show one task")
    show_p.add_argument("task_id")

    complete_p = sub.add_parser("complete", help="Manually complete (admin)")
    complete_p.add_argument("task_id")

    sub.add_parser("cleanup", help="Mark expired tasks + prune old ones")

    args = parser.parse_args()
    if args.tasks_subcommand is None:
        parser.print_help()
        return 2

    _ensure_plugin_loaded()
    from hermes_plugins.nurture_community.cli import _cmd_tasks
    return _cmd_tasks(args)


if __name__ == "__main__":
    raise SystemExit(main())
