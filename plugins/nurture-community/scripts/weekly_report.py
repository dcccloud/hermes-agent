#!/usr/bin/env python3
"""Cron wrapper — prints the weekly community report to stdout.

Designed to be invoked via ``hermes cron`` (see weekly schedule below).

Usage::

    # One-off
    python -m plugins.nurture_community.scripts.weekly_report

    # As a hermes cron job (weekly Monday 9am, delivered to telegram)
    hermes cron create "0 9 * * 1" \\
      "Avatar-Hermes weekly community report" \\
      --script $(python -c "import hermes_plugins.nurture_community as p, os; print(os.path.join(os.path.dirname(p.__file__), 'scripts', 'weekly_report.py'))") \\
      --deliver telegram \\
      --name "nurture weekly report"

The script prints ``[SILENT] ...`` when there is no community activity
in the window — hermes cron's silent-pattern delivery skips the
notification entirely so users don't get pinged on quiet weeks.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


def _ensure_plugin_loaded() -> None:
    """Load the nurture-community plugin module via importlib (the
    directory has a hyphen and isn't a regular Python package)."""
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
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin from {plugin_dir}")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = module_name
    mod.__path__ = [str(plugin_dir)]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)


def main() -> int:
    _ensure_plugin_loaded()

    from hermes_plugins.nurture_community.cli import _build_standalone_engine

    engine = _build_standalone_engine()
    if engine is None:
        print("[SILENT] nurture-community: cannot reach KnowledgeEngine")
        return 1

    async def run() -> int:
        await engine.init()
        from hermes_plugins.nurture_community.analytics import build_weekly_report
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


if __name__ == "__main__":
    raise SystemExit(main())
