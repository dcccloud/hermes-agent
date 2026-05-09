"""RecipeStore (JSON backend) — community recipe aggregation, per-node
statistics, and version management.

Handles recipe ingestion from capability updates, explicit registration,
deduplication, and best-recipe selection.

PG backend: ``plugins/nurture-community/pg/pg_recipe_store.py``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ._helpers import load_json, now_ms, save_json


# ---------------------------------------------------------------------------
# Legacy import rewrites — applied on ingestion to keep stored code clean
# ---------------------------------------------------------------------------

_IMPORT_REWRITES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\boperation_engine\.learning\.humanize\b"),
     "openclaw_agent.engine.learning.humanize"),
]


def sanitize_recipe_code(code: str) -> str:
    """Apply legacy import rewrites to recipe code on ingestion."""
    for pattern, replacement in _IMPORT_REWRITES:
        code = pattern.sub(replacement, code)
    return code


def recipe_key(app: str, operation: str, step: str) -> str:
    return f"{app}/{operation}/{step}"


def compute_code_hash(code: str) -> str:
    """sha256(code)[:12] — matches OpenClaw devices' meta.json version field."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class NodeStats:
    success: int = 0
    failure: int = 0
    lastReportedAt: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NodeStats":
        return cls(
            success=int(data.get("success", 0) or 0),
            failure=int(data.get("failure", 0) or 0),
            lastReportedAt=int(data.get("lastReportedAt", 0) or 0),
        )


@dataclass
class RecipeEntry:
    app: str
    operation: str
    step: str
    version: str
    code: str
    originNodeId: str
    originDeviceModel: str
    createdAt: int
    nodeStats: Dict[str, NodeStats] = field(default_factory=dict)
    globalSuccessRate: float = 0.0
    globalSampleCount: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecipeEntry":
        return cls(
            app=data.get("app", ""),
            operation=data.get("operation", ""),
            step=data.get("step", ""),
            version=data.get("version", ""),
            code=data.get("code", ""),
            originNodeId=data.get("originNodeId", ""),
            originDeviceModel=data.get("originDeviceModel", ""),
            createdAt=int(data.get("createdAt", 0) or 0),
            nodeStats={
                k: NodeStats.from_dict(v)
                for k, v in (data.get("nodeStats") or {}).items()
                if isinstance(v, dict)
            },
            globalSuccessRate=float(data.get("globalSuccessRate", 0) or 0),
            globalSampleCount=int(data.get("globalSampleCount", 0) or 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app": self.app,
            "operation": self.operation,
            "step": self.step,
            "version": self.version,
            "code": self.code,
            "originNodeId": self.originNodeId,
            "originDeviceModel": self.originDeviceModel,
            "createdAt": self.createdAt,
            "nodeStats": {k: asdict(v) for k, v in self.nodeStats.items()},
            "globalSuccessRate": self.globalSuccessRate,
            "globalSampleCount": self.globalSampleCount,
        }


def _recompute_global_stats(entry: RecipeEntry) -> None:
    total_success = sum(s.success for s in entry.nodeStats.values())
    total_failure = sum(s.failure for s in entry.nodeStats.values())
    entry.globalSampleCount = total_success + total_failure
    entry.globalSuccessRate = (
        total_success / entry.globalSampleCount
        if entry.globalSampleCount > 0
        else 0.0
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class RecipeStore:
    def __init__(self, store_dir: Path):
        self._store_file = Path(store_dir) / "recipes.json"
        raw = load_json(self._store_file, [])
        self._recipes: List[RecipeEntry] = [
            RecipeEntry.from_dict(r) for r in raw if isinstance(r, dict)
        ]
        self._index: Dict[str, List[RecipeEntry]] = {}
        self._rebuild_index()

    @property
    def all(self) -> Sequence[RecipeEntry]:
        return tuple(self._recipes)

    def ingest_from_capability(
        self,
        node_id: str,
        device_model: str,
        apps: Dict[str, Any],
    ) -> bool:
        """Ingest recipe stats + optional code from a node's capability update.
        Returns True if anything changed (and was persisted).
        """
        changed = False

        for app, app_data in (apps or {}).items():
            if not isinstance(app_data, dict):
                continue
            for op in app_data.get("operations", []) or []:
                if not isinstance(op, dict):
                    continue
                op_name = op.get("name", "")
                for step_info in op.get("steps", []) or []:
                    if not isinstance(step_info, dict):
                        continue
                    if not step_info.get("has_recipe"):
                        continue
                    key = recipe_key(app, op_name, step_info.get("step", ""))
                    entries = self._index.get(key, [])

                    found = False
                    for entry in entries:
                        if (
                            entry.originNodeId == node_id
                            or node_id in entry.nodeStats
                        ):
                            entry.nodeStats[node_id] = NodeStats(
                                success=int(step_info.get("success", 0) or 0),
                                failure=int(step_info.get("failure", 0) or 0),
                                lastReportedAt=now_ms(),
                            )
                            new_code = step_info.get("code")
                            if new_code and entry.code != new_code:
                                entry.code = sanitize_recipe_code(new_code)
                                entry.version = (
                                    f"node-{node_id}-{compute_code_hash(entry.code)}"
                                )
                            _recompute_global_stats(entry)
                            found = True
                            changed = True

                    if not found:
                        raw_code = step_info.get("code") or ""
                        code = sanitize_recipe_code(raw_code) if raw_code else ""
                        version = (
                            f"node-{node_id}-{compute_code_hash(code)}"
                            if code
                            else f"node-{node_id}"
                        )
                        success = int(step_info.get("success", 0) or 0)
                        failure = int(step_info.get("failure", 0) or 0)
                        entry = RecipeEntry(
                            app=app,
                            operation=op_name,
                            step=step_info.get("step", ""),
                            version=version,
                            code=code,
                            originNodeId=node_id,
                            originDeviceModel=device_model,
                            createdAt=now_ms(),
                            nodeStats={
                                node_id: NodeStats(
                                    success=success,
                                    failure=failure,
                                    lastReportedAt=now_ms(),
                                )
                            },
                            globalSuccessRate=float(step_info.get("success_rate", 0) or 0),
                            globalSampleCount=success + failure,
                        )
                        self._recipes.append(entry)
                        self._index.setdefault(key, []).append(entry)
                        changed = True

        if changed:
            self._save()
        return changed

    def register_recipe(
        self,
        *,
        app: str,
        operation: str,
        step: str,
        code: str,
        node_id: str,
        device_model: str,
    ) -> RecipeEntry:
        """Register a recipe with full code (deduplicates by version hash)."""
        clean_code = sanitize_recipe_code(code)
        version = compute_code_hash(clean_code)
        key = recipe_key(app, operation, step)
        entries = self._index.get(key, [])

        for existing in entries:
            if existing.version == version:
                return existing

        entry = RecipeEntry(
            app=app,
            operation=operation,
            step=step,
            version=version,
            code=clean_code,
            originNodeId=node_id,
            originDeviceModel=device_model,
            createdAt=now_ms(),
            nodeStats={},
            globalSuccessRate=0.0,
            globalSampleCount=0,
        )
        self._recipes.append(entry)
        self._index.setdefault(key, []).append(entry)
        self._save()
        return entry

    def store_fused_recipe(
        self,
        *,
        app: str,
        operation: str,
        step: str,
        code: str,
    ) -> RecipeEntry:
        """LLM-fused recipe — node id 'fused', model 'canonical'."""
        return self.register_recipe(
            app=app,
            operation=operation,
            step=step,
            code=code,
            node_id="fused",
            device_model="canonical",
        )

    # -- queries -----------------------------------------------------------

    def get_versions(self, app: str, operation: str, step: str) -> List[RecipeEntry]:
        return list(self._index.get(recipe_key(app, operation, step), []))

    def get_global_success_rate(self, app: str, operation: str, step: str) -> float:
        entries = self.get_versions(app, operation, step)
        if not entries:
            return 0.0
        total_success = 0
        total_samples = 0
        for e in entries:
            for s in e.nodeStats.values():
                total_success += s.success
                total_samples += s.success + s.failure
        return total_success / total_samples if total_samples else 0.0

    def get_best_recipe(
        self, app: str, operation: str, step: str
    ) -> Optional[RecipeEntry]:
        entries = self.get_versions(app, operation, step)
        best: Optional[RecipeEntry] = None
        for e in entries:
            if not e.code:
                continue
            if best is None or e.globalSuccessRate > best.globalSuccessRate:
                best = e
        return best

    def get_fusion_candidates(self) -> List[Dict[str, Any]]:
        """Steps with >= 2 distinct code versions and total samples >= 5."""
        candidates: List[Dict[str, Any]] = []
        for entries in self._index.values():
            with_code = [e for e in entries if e.code]
            if len(with_code) < 2:
                continue
            total_samples = sum(e.globalSampleCount for e in with_code)
            if total_samples < 5:
                continue
            head = with_code[0]
            candidates.append({
                "app": head.app,
                "operation": head.operation,
                "step": head.step,
                "entries": with_code,
            })
        return candidates

    # -- internal -----------------------------------------------------------

    def _save(self) -> None:
        save_json(self._store_file, [r.to_dict() for r in self._recipes])

    def _rebuild_index(self) -> None:
        self._index.clear()
        for r in self._recipes:
            self._index.setdefault(recipe_key(r.app, r.operation, r.step), []).append(r)
