#!/usr/bin/env python3
"""Foreground entrypoint for the community FastAPI + MCP server.

Usage::

    python -m plugins.nurture_community.scripts.serve --host 127.0.0.1 --port 18790

Or via the wrapper at ``start-example/start-community.sh``.

This is the script-friendly path. The same code runs when the
plugin's on_session_start hook fires inside an interactive hermes
session — but for a long-running service you typically want a plain
Python process you can supervise (systemd / launchd / Docker).
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
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin from {plugin_dir}")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = module_name
    mod.__path__ = [str(plugin_dir)]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nurture-community-serve",
        description=(
            "Run the Avatar-Hermes community FastAPI + MCP server in the "
            "foreground. Mirrors `hermes nurture-community serve` (which "
            "is gated by the upstream hermes argparse limitation)."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18790)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()

    _ensure_plugin_loaded()
    from hermes_plugins.nurture_community.cli import _cmd_serve
    return _cmd_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
