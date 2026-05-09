"""Bootstrap the nurture-community plugin under ``hermes_plugins.*`` so
test modules can ``import hermes_plugins.nurture_community...`` directly.

Mirrors hermes ``PluginManager._load_local_module()``: the plugin
directory contains a hyphen and cannot be imported as a regular Python
package, so we use ``importlib.util.spec_from_file_location`` with the
namespace ``hermes_plugins.nurture_community``.
"""
from __future__ import annotations

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

    plugin_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "plugins" / "nurture-community"
    )
    spec = importlib.util.spec_from_file_location(
        module_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec is not None and spec.loader is not None, (
        "Failed to build plugin module spec — is plugins/nurture-community/ present?"
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = module_name
    mod.__path__ = [str(plugin_dir)]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)


_ensure_plugin_loaded()
