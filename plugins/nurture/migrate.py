"""OpenClaw → Avatar-Hermes data migration.

Per ADR-005 (Recipe stays independent of Hermes Skills) the migration
is **pure file copy** — no schema transformation. We only remap paths:

    ~/.openclaw/nurture-workspaces/...  →  ~/.hermes/nurture/...
    ~/.openclaw/history/...              →  ~/.hermes/nurture/history/...

See docs/avatar-hermes/migration.md for the full spec.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path mapping
# ---------------------------------------------------------------------------


@dataclass
class PathMapping:
    """Single file/dir to migrate."""
    source: Path     # absolute path under <openclaw_root>
    dest: Path       # absolute path under <hermes_workspace>
    kind: str        # "file" | "dir"
    category: str    # "agent" | "data/recipes" | "data/app_graphs" | ...


@dataclass
class MigrationPlan:
    """What the migrate command will do."""
    source_root: Path
    dest_root: Path
    mappings: List[PathMapping] = field(default_factory=list)
    skipped_apps: List[str] = field(default_factory=list)
    conflicts: List[Path] = field(default_factory=list)

    def total_items(self) -> int:
        return len(self.mappings)


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


_AGENT_FILES = (
    "goals.md",
    "community-tasks.json",
    "community-recipes-cache.json",
    "value-schema.json",
    "advice.json",
    "directives.json",
)


def build_plan(
    source: Path,
    dest: Path,
    *,
    skip_recipes: bool = False,
    only_app: Optional[str] = None,
) -> MigrationPlan:
    """Walk *source* and enumerate items to migrate.

    Args:
        source: OpenClaw home (e.g. ``~/.openclaw``).
        dest: Hermes nurture workspace (e.g. ``~/.hermes/nurture``).
        skip_recipes: Skip ``data/recipes/`` and ``data/traces/``.
        only_app: Only include data for one app under ``data/{recipes,traces,app_graphs}``.
    """
    plan = MigrationPlan(source_root=source, dest_root=dest)

    workspaces = source / "nurture-workspaces"
    if not workspaces.exists():
        # Try alternate layouts (some users have a different config)
        if (source / "agent").exists():
            workspaces = source
        else:
            return plan  # nothing to migrate

    # 1. agent/ — flat files (goals.md, community-tasks.json, etc.)
    agent_src = workspaces / "agent"
    if agent_src.exists():
        for name in _AGENT_FILES:
            src = agent_src / name
            if src.exists():
                plan.mappings.append(PathMapping(
                    source=src,
                    dest=dest / "agent" / name,
                    kind="file",
                    category="agent",
                ))
        # 2. agent/persona/*.md
        persona_src = agent_src / "persona"
        if persona_src.exists():
            for path in sorted(persona_src.glob("*.md")):
                plan.mappings.append(PathMapping(
                    source=path,
                    dest=dest / "agent" / "persona" / path.name,
                    kind="file",
                    category="agent/persona",
                ))

    # 3. data/recipes/<app>/<op>/<step>/* — entire directory tree per app
    if not skip_recipes:
        recipes_src = workspaces / "data" / "recipes"
        if recipes_src.exists():
            for app_dir in sorted(p for p in recipes_src.iterdir() if p.is_dir()):
                if only_app and app_dir.name != only_app:
                    plan.skipped_apps.append(app_dir.name)
                    continue
                plan.mappings.append(PathMapping(
                    source=app_dir,
                    dest=dest / "data" / "recipes" / app_dir.name,
                    kind="dir",
                    category="data/recipes",
                ))

        # 4. data/traces/<app>/*.jsonl
        traces_src = workspaces / "data" / "traces"
        if traces_src.exists():
            for app_dir in sorted(p for p in traces_src.iterdir() if p.is_dir()):
                if only_app and app_dir.name != only_app:
                    continue
                plan.mappings.append(PathMapping(
                    source=app_dir,
                    dest=dest / "data" / "traces" / app_dir.name,
                    kind="dir",
                    category="data/traces",
                ))

    # 5. data/app_graphs/*.json
    graphs_src = workspaces / "data" / "app_graphs"
    if graphs_src.exists():
        for path in sorted(graphs_src.glob("*.json")):
            if only_app and path.stem != only_app:
                continue
            plan.mappings.append(PathMapping(
                source=path,
                dest=dest / "data" / "app_graphs" / path.name,
                kind="file",
                category="data/app_graphs",
            ))

    # 6. data/events/* — events don't have an app subdirectory; copy entire tree
    events_src = workspaces / "data" / "events"
    if events_src.exists() and not skip_recipes:
        for path in sorted(events_src.iterdir()):
            plan.mappings.append(PathMapping(
                source=path,
                dest=dest / "data" / "events" / path.name,
                kind="dir" if path.is_dir() else "file",
                category="data/events",
            ))

    # 7. history/profiles/<account>/*
    history_src = source / "history"
    if history_src.exists():
        plan.mappings.append(PathMapping(
            source=history_src,
            dest=dest / "history",
            kind="dir",
            category="history",
        ))

    # Conflicts — items that already exist in dest
    plan.conflicts = [
        m.dest for m in plan.mappings if m.dest.exists()
    ]

    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class MigrationResult:
    items_copied: int = 0
    items_skipped: int = 0
    items_failed: int = 0
    bytes_copied: int = 0
    failures: List[str] = field(default_factory=list)


def execute_plan(
    plan: MigrationPlan,
    *,
    overwrite: bool = False,
) -> MigrationResult:
    """Execute the plan. Returns a summary of what happened."""
    result = MigrationResult()

    for mapping in plan.mappings:
        try:
            if mapping.dest.exists() and not overwrite:
                result.items_skipped += 1
                continue
            mapping.dest.parent.mkdir(parents=True, exist_ok=True)
            if mapping.kind == "file":
                shutil.copy2(mapping.source, mapping.dest)
                result.bytes_copied += mapping.source.stat().st_size
            else:
                if mapping.dest.exists() and overwrite:
                    shutil.rmtree(mapping.dest)
                shutil.copytree(
                    mapping.source, mapping.dest,
                    dirs_exist_ok=False,
                )
                result.bytes_copied += sum(
                    f.stat().st_size for f in mapping.dest.rglob("*") if f.is_file()
                )
            result.items_copied += 1
        except Exception as e:
            result.items_failed += 1
            result.failures.append(f"{mapping.source} → {mapping.dest}: {e}")
            logger.warning("migrate: failed %s: %s", mapping.source, e)

    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_migration(dest: Path) -> List[str]:
    """Sanity-check the migrated layout. Returns a list of warnings
    (non-empty list means the operator should investigate)."""
    warnings: List[str] = []

    # All meta.json files should parse
    recipes_root = dest / "data" / "recipes"
    if recipes_root.exists():
        for meta in recipes_root.rglob("*.meta.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                warnings.append(f"meta.json parse failed: {meta}: {e}")
                continue
            # Common OpenClaw fields we expect
            for required in ("version", "source"):
                if required not in data:
                    warnings.append(f"meta.json missing {required!r}: {meta}")

    # Recipe pairing: every main.py should have a main.meta.json
    if recipes_root.exists():
        for py in recipes_root.rglob("*.py"):
            if py.name in ("__init__.py", "_helpers.py"):
                continue
            meta_path = py.with_suffix(".meta.json")
            if not meta_path.exists():
                warnings.append(f"recipe missing meta.json: {py}")

    # JSON files should be UTF-8 + valid JSON
    for relative in (
        "agent/community-tasks.json",
        "agent/community-recipes-cache.json",
        "agent/value-schema.json",
        "agent/advice.json",
        "agent/directives.json",
    ):
        path = dest / relative
        if not path.exists():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            warnings.append(f"{relative}: {e}")

    return warnings


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------


def migrate(
    source: Path,
    dest: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    skip_recipes: bool = False,
    only_app: Optional[str] = None,
) -> MigrationResult:
    """Run the full migration pipeline.

    On dry-run, returns an empty MigrationResult (caller should inspect
    the plan instead).
    """
    plan = build_plan(
        source, dest,
        skip_recipes=skip_recipes,
        only_app=only_app,
    )
    if dry_run:
        return MigrationResult()
    return execute_plan(plan, overwrite=overwrite)


def expand_user(path: str) -> Path:
    return Path(path).expanduser().resolve()
