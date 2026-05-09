"""Phase 4: kanban_bridge end-to-end test.

Verifies that:
  1. ``maybe_dispatch_new_tasks`` reads ``community-tasks.json``,
     dispatches ``status=received`` tasks to Hermes Kanban, and updates
     the file with ``kanban_task_id`` + ``status=running``.
  2. The bridge is idempotent — re-running on the same file is a no-op
     (idempotency_key prevents double-create).
  3. The created kanban task has the expected title / body / assignee /
     priority shape.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


def _bootstrap_nurture_plugin() -> None:
    """Mirror conftest's plugin loader for the device-side plugin too."""
    import importlib.util
    import sys
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
        Path(__file__).resolve().parent.parent.parent
        / "plugins" / "nurture"
    )
    spec = importlib.util.spec_from_file_location(
        f"{parent}.nurture",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = f"{parent}.nurture"
    mod.__path__ = [str(plugin_dir)]
    sys.modules[f"{parent}.nurture"] = mod
    spec.loader.exec_module(mod)


@pytest.fixture(autouse=True)
def isolated_kanban_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point hermes Kanban at a temp ~/.hermes so each test gets a fresh DB."""
    home = tmp_path / "fake-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    # hermes_cli.config caches via module-level vars — clear it
    import hermes_cli.config as _config
    if hasattr(_config, "_CONFIG_CACHE"):
        _config._CONFIG_CACHE = None
    return home


def test_dispatch_creates_kanban_card(isolated_kanban_db: Path) -> None:
    _bootstrap_nurture_plugin()
    from hermes_plugins.nurture.kanban_bridge import maybe_dispatch_new_tasks

    workspace = isolated_kanban_db / "workspace"
    tasks_file = workspace / "agent" / "community-tasks.json"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text(
        json.dumps([{
            "taskId": "task-12345",
            "app": "douyin",
            "description": "Like 5 videos",
            "priority": "normal",
            "expiresAt": "2026-12-31T23:59:59Z",
            "createdAt": 1762430400000,
            "receivedAt": 1762430500000,
            "status": "received",
        }]),
        encoding="utf-8",
    )

    count = maybe_dispatch_new_tasks(tasks_file, worker_profile="nurture-test-worker")
    assert count == 1

    # File updated in place
    after = json.loads(tasks_file.read_text(encoding="utf-8"))
    assert len(after) == 1
    assert after[0]["status"] == "running"
    kanban_task_id = after[0].get("kanban_task_id")
    assert kanban_task_id  # opaque ID format from kanban_db.create_task

    # Card exists in kanban DB with the expected shape
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        task = kb.get_task(conn, kanban_task_id)
    assert task is not None
    assert task.title.startswith("[Community Task task-12345]")
    assert task.assignee == "nurture-test-worker"
    assert "nurture_task_report" in (task.body or "")
    assert "task-12345" in (task.body or "")


def test_dispatch_is_idempotent(isolated_kanban_db: Path) -> None:
    _bootstrap_nurture_plugin()
    from hermes_plugins.nurture.kanban_bridge import dispatch_to_kanban

    task = {
        "taskId": "task-idem",
        "app": "douyin",
        "description": "Like 1 video",
        "priority": "normal",
        "expiresAt": "2026-12-31T23:59:59Z",
        "createdAt": 1762430400000,
    }
    first = dispatch_to_kanban(task, worker_profile="nurture-test-worker")
    second = dispatch_to_kanban(task, worker_profile="nurture-test-worker")
    assert first is not None
    assert first == second  # idempotency_key dedupes


def test_high_priority_propagates(isolated_kanban_db: Path) -> None:
    _bootstrap_nurture_plugin()
    from hermes_plugins.nurture.kanban_bridge import dispatch_to_kanban

    high_id = dispatch_to_kanban(
        {
            "taskId": "task-high",
            "app": "douyin", "description": "urgent",
            "priority": "high",
            "expiresAt": "2026-12-31T23:59:59Z",
            "createdAt": 1762430400000,
        },
        worker_profile="nurture-test-worker",
    )
    normal_id = dispatch_to_kanban(
        {
            "taskId": "task-normal",
            "app": "douyin", "description": "regular",
            "priority": "normal",
            "expiresAt": "2026-12-31T23:59:59Z",
            "createdAt": 1762430400000,
        },
        worker_profile="nurture-test-worker",
    )

    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        high = kb.get_task(conn, high_id)
        normal = kb.get_task(conn, normal_id)
    assert high.priority == 10
    assert normal.priority == 0


def test_dispatch_skips_already_dispatched(isolated_kanban_db: Path) -> None:
    _bootstrap_nurture_plugin()
    from hermes_plugins.nurture.kanban_bridge import maybe_dispatch_new_tasks

    workspace = isolated_kanban_db / "workspace2"
    tasks_file = workspace / "agent" / "community-tasks.json"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text(
        json.dumps([{
            "taskId": "task-already",
            "app": "douyin", "description": "x",
            "priority": "normal",
            "expiresAt": "2026-12-31T23:59:59Z",
            "createdAt": 1762430400000,
            "receivedAt": 1762430500000,
            "status": "running",
            "kanban_task_id": "kt_existing",  # already dispatched
        }]),
        encoding="utf-8",
    )
    count = maybe_dispatch_new_tasks(tasks_file)
    assert count == 0
