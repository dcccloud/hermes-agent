#!/usr/bin/env python3
"""Foreground entrypoint for the device-side nurture stack.

Usage::

    python -m plugins.nurture.scripts.serve [--no-community]

Or via the wrapper at ``start-example/start-agent.sh``.

This spawns the Python device service (FastAPI on :8600 by default,
configured via ``plugins.nurture.serverPort`` in ~/.hermes/config.yaml)
and — if ``community.url`` is set — the community connector background
thread. Blocks on SIGINT/SIGTERM.
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

    module_name = f"{parent}.nurture"
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
        prog="nurture-serve",
        description=(
            "Run the Avatar-Hermes device-side stack (Python device service "
            "+ community connector) in the foreground."
        ),
    )
    parser.add_argument(
        "--no-community", action="store_true",
        help="Skip community connector even if community.url is configured",
    )
    args = parser.parse_args()

    _ensure_plugin_loaded()
    from hermes_plugins.nurture.cli import _cmd_serve
    return _cmd_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
