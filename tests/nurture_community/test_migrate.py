"""Phase 6 migration tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


def _bootstrap() -> None:
    """Mirror the conftest pattern for the device-side plugin."""
    import importlib.util
    import types

    parent = "hermes_plugins"
    if parent not in sys.modules:
        ns = types.ModuleType(parent)
        ns.__path__ = []
        ns.__package__ = parent
        sys.modules[parent] = ns
    if f"{parent}.nurture" in sys.modules:
        return
    plugin_dir = (
        Path(__file__).resolve().parent.parent.parent / "plugins" / "nurture"
    )
    spec = importlib.util.spec_from_file_location(
        f"{parent}.nurture",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = f"{parent}.nurture"
    mod.__path__ = [str(plugin_dir)]
    sys.modules[f"{parent}.nurture"] = mod
    spec.loader.exec_module(mod)


_bootstrap()
from hermes_plugins.nurture.migrate import (
    build_plan,
    execute_plan,
    migrate,
    validate_migration,
)


def _seed_openclaw(root: Path) -> None:
    """Build a fake OpenClaw home with realistic file shapes."""
    workspaces = root / "nurture-workspaces"
    agent = workspaces / "agent"
    persona = agent / "persona"
    recipes = workspaces / "data" / "recipes"
    traces = workspaces / "data" / "traces"
    graphs = workspaces / "data" / "app_graphs"
    history = root / "history" / "profiles" / "alice"

    for d in (agent, persona, recipes, traces, graphs, history):
        d.mkdir(parents=True, exist_ok=True)

    (agent / "goals.md").write_text("# Goals\n", encoding="utf-8")
    (agent / "community-tasks.json").write_text("[]", encoding="utf-8")
    (agent / "community-recipes-cache.json").write_text(
        json.dumps({"updatedAt": 0, "recipes": []}),
        encoding="utf-8",
    )
    (persona / "douyin.md").write_text("# douyin persona\n", encoding="utf-8")
    (persona / "xingtu.md").write_text("# xingtu persona\n", encoding="utf-8")

    # Recipe tree: data/recipes/douyin/give_a_like/main/{main.py,main.meta.json}
    rdir = recipes / "douyin" / "give_a_like" / "main"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "main.py").write_text("async def execute(d): pass\n", encoding="utf-8")
    (rdir / "main.meta.json").write_text(
        json.dumps({
            "version": "abc123def456", "source": "self",
            "success": 10, "failure": 1,
        }),
        encoding="utf-8",
    )

    # Trace JSONL
    (traces / "douyin").mkdir(parents=True, exist_ok=True)
    (traces / "douyin" / "give_a_like.jsonl").write_text(
        '{"id":"t1","success":true}\n', encoding="utf-8",
    )

    # App graph
    (graphs / "douyin.json").write_text(
        json.dumps({"states": [{"state_id": "home"}]}),
        encoding="utf-8",
    )

    # History
    (history / "latest.json").write_text(
        json.dumps({"followers": 1234}),
        encoding="utf-8",
    )


def test_build_plan_finds_all_categories(tmp_path: Path) -> None:
    src = tmp_path / "openclaw"
    dst = tmp_path / "hermes-nurture"
    _seed_openclaw(src)

    plan = build_plan(src, dst)
    categories = {m.category for m in plan.mappings}
    assert "agent" in categories
    assert "agent/persona" in categories
    assert "data/recipes" in categories
    assert "data/traces" in categories
    assert "data/app_graphs" in categories
    assert "history" in categories
    assert plan.total_items() > 5


def test_build_plan_skip_recipes(tmp_path: Path) -> None:
    src = tmp_path / "openclaw"
    dst = tmp_path / "hermes-nurture"
    _seed_openclaw(src)

    plan = build_plan(src, dst, skip_recipes=True)
    categories = {m.category for m in plan.mappings}
    assert "data/recipes" not in categories
    assert "data/traces" not in categories
    # Other categories remain
    assert "data/app_graphs" in categories
    assert "agent/persona" in categories


def test_build_plan_only_app(tmp_path: Path) -> None:
    src = tmp_path / "openclaw"
    dst = tmp_path / "hermes-nurture"
    _seed_openclaw(src)
    # Add a second app's recipe so we can test filtering
    (src / "nurture-workspaces" / "data" / "recipes" / "facebook" /
     "open_app" / "main").mkdir(parents=True)
    (src / "nurture-workspaces" / "data" / "recipes" / "facebook" /
     "open_app" / "main" / "main.py").write_text("# fb", encoding="utf-8")

    plan = build_plan(src, dst, only_app="douyin")
    recipe_apps = {m.source.name for m in plan.mappings if m.category == "data/recipes"}
    assert recipe_apps == {"douyin"}
    assert "facebook" in plan.skipped_apps


def test_execute_plan_copies_files(tmp_path: Path) -> None:
    src = tmp_path / "openclaw"
    dst = tmp_path / "hermes-nurture"
    _seed_openclaw(src)

    plan = build_plan(src, dst)
    result = execute_plan(plan)
    assert result.items_failed == 0
    assert result.items_copied > 0
    assert result.bytes_copied > 0

    # Spot check
    assert (dst / "agent" / "goals.md").exists()
    assert (dst / "agent" / "persona" / "douyin.md").exists()
    assert (dst / "data" / "recipes" / "douyin" / "give_a_like" / "main" / "main.py").exists()
    assert (dst / "data" / "app_graphs" / "douyin.json").exists()
    assert (dst / "history" / "profiles" / "alice" / "latest.json").exists()


def test_execute_plan_no_overwrite_by_default(tmp_path: Path) -> None:
    src = tmp_path / "openclaw"
    dst = tmp_path / "hermes-nurture"
    _seed_openclaw(src)

    # Pre-create a conflicting file at dest
    (dst / "agent").mkdir(parents=True)
    (dst / "agent" / "goals.md").write_text("ALREADY-EXISTS", encoding="utf-8")

    plan = build_plan(src, dst)
    result = execute_plan(plan)
    # The conflicting file is NOT touched
    assert (dst / "agent" / "goals.md").read_text(encoding="utf-8") == "ALREADY-EXISTS"
    # Skip count includes that file
    assert result.items_skipped >= 1


def test_execute_plan_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "openclaw"
    dst = tmp_path / "hermes-nurture"
    _seed_openclaw(src)
    (dst / "agent").mkdir(parents=True)
    (dst / "agent" / "goals.md").write_text("STALE", encoding="utf-8")

    plan = build_plan(src, dst)
    execute_plan(plan, overwrite=True)
    assert (dst / "agent" / "goals.md").read_text(encoding="utf-8") == "# Goals\n"


def test_validate_migration_passes_on_clean_copy(tmp_path: Path) -> None:
    src = tmp_path / "openclaw"
    dst = tmp_path / "hermes-nurture"
    _seed_openclaw(src)
    migrate(src, dst)
    warnings = validate_migration(dst)
    assert warnings == []


def test_validate_migration_flags_orphan_recipe(tmp_path: Path) -> None:
    """A main.py without main.meta.json is a corrupt migration."""
    dst = tmp_path / "hermes-nurture"
    rdir = dst / "data" / "recipes" / "x" / "y" / "main"
    rdir.mkdir(parents=True)
    (rdir / "main.py").write_text("# orphan", encoding="utf-8")
    # Note: NO main.meta.json
    warnings = validate_migration(dst)
    assert any("missing meta.json" in w for w in warnings)


def test_dry_run_does_not_copy(tmp_path: Path) -> None:
    src = tmp_path / "openclaw"
    dst = tmp_path / "hermes-nurture"
    _seed_openclaw(src)
    result = migrate(src, dst, dry_run=True)
    assert result.items_copied == 0
    assert not (dst / "agent" / "goals.md").exists()
