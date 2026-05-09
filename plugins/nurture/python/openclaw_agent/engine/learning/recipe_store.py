"""Manage LLM-generated Python recipe files.

Each recipe is a ``.py`` file under ``data/recipes/<operation>/<step>.py``
that exposes ``execute(device) -> bool`` and optionally
``verify(device) -> bool``.  RecipeStore handles:

* dynamic loading via ``importlib``
* per-recipe success/failure statistics (sidecar ``.meta.json``)
* auto-disabling when failure rate exceeds a threshold
"""

import hashlib
import importlib.util
import json
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("recipe_store")

_RECIPE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "recipes"
_DISABLE_THRESHOLD = 0.5  # disable recipe after > 50 % failure
_MIN_SAMPLES = 3          # need at least N executions before disabling
_DENIED_LIST_MAX = 10     # cap on denied_community_versions to prevent bloat


def _content_version(code: str) -> str:
    """Stable identifier of recipe code, matching community's hash format."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]


class RecipeModule:
    """Thin wrapper around a loaded recipe module.

    Every recipe must have ``execute(device) -> bool``.
    Optionally it may also define ``verify(device) -> bool``
    for independent post-execution verification (e.g. detect_page
    check).  When absent, AdaptiveStep falls back to trusting
    the execute() return value.
    """

    def __init__(self, path: Path, mod: ModuleType):
        self.path = path
        self.module = mod
        self.execute: Callable[..., bool] = getattr(mod, "execute")
        self.verify: Callable[..., bool] | None = getattr(mod, "verify", None)


class RecipeStore:
    """Manages recipe files and their runtime statistics."""

    def __init__(self, recipe_dir: str | Path | None = None):
        self._dir = Path(recipe_dir) if recipe_dir else _RECIPE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def _recipe_path(self, operation: str, step: str) -> Path:
        return self._dir / operation / f"{step}.py"

    def _meta_path(self, operation: str, step: str) -> Path:
        return self._dir / operation / f"{step}.meta.json"

    def _previous_path(self, operation: str, step: str) -> Path:
        """Backup slot for the last stable version (used for rollback)."""
        return self._dir / operation / f"{step}.previous.py"

    def _previous_meta_path(self, operation: str, step: str) -> Path:
        return self._dir / operation / f"{step}.previous.meta.json"

    def get(self, operation: str, step: str) -> RecipeModule | None:
        """Load the recipe for *(operation, step)* if it exists and is
        not disabled.  Returns ``None`` when no usable recipe is found.

        Before loading, opportunistically syncs with the community
        recipe cache so a freshly-arrived better version takes effect
        without waiting for the next reviewer pass.
        """
        py_path = self._recipe_path(operation, step)
        if not py_path.exists():
            return None

        # Best-effort: pull a community-better version if one is available
        # since we last looked at the cache. Cheap (one mtime stat + a
        # JSON read on cache-bump events; thresholds inside maybe_adopt
        # filter out noise). Errors are swallowed — execution must
        # continue even if the sync fails.
        self._maybe_sync_with_community(operation, step)

        meta = self._read_meta(operation, step)
        if meta.get("disabled"):
            logger.debug(f"Recipe {operation}/{step} is disabled")
            return None

        try:
            spec = importlib.util.spec_from_file_location(
                f"recipe.{operation}.{step}", py_path,
            )
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "execute"):
                logger.warning(f"Recipe {py_path} has no execute()")
                return None
            return RecipeModule(py_path, mod)
        except Exception:
            logger.exception(f"Failed to load recipe {py_path}")
            return None

    # -- community sync ------------------------------------------------

    def _maybe_sync_with_community(self, operation: str, step: str) -> None:
        """Consult the community recipe cache and adopt a better version
        if one is available since the last check.

        Cheap path: cache file mtime <= meta.last_community_check → no-op.
        Otherwise: look up best community recipe; if `maybe_adopt_community_recipe`
        accepts (it has its own threshold gating), it'll overwrite the
        local recipe via `save()`. We then record the cache mtime in
        meta.last_community_check so subsequent calls skip until the
        connector writes a fresh cache.
        """
        try:
            from openclaw_agent.engine.learning.community_recipe import (
                _resolve_cache_path,
                get_community_best_recipe,
                maybe_adopt_community_recipe,
            )
        except Exception:
            return

        try:
            cache_path = _resolve_cache_path()
            if not cache_path.exists():
                return
            cache_mtime = cache_path.stat().st_mtime
        except OSError:
            return

        meta = self._read_meta(operation, step)
        last_check = meta.get("last_community_check", 0.0)
        if cache_mtime <= last_check:
            return

        # Operation paths look like "facebook/like_video_1"; first segment
        # is the app namespace the community indexes by.
        app = operation.split("/", 1)[0] if "/" in operation else ""
        if not app:
            # Without a namespaced operation we can't look up community.
            # Still record the check so we don't re-attempt every call.
            self.update_meta(operation, step, {"last_community_check": cache_mtime})
            return

        try:
            community = get_community_best_recipe(app, operation, step)
        except Exception:
            self.update_meta(operation, step, {"last_community_check": cache_mtime})
            return

        if community:
            community_version = community.get("version", "")
            denied = meta.get("denied_community_versions", [])
            if community_version and community_version in denied:
                logger.info(
                    f"Skipping community recipe {community_version} for "
                    f"{operation}/{step}: previously rolled back from this version"
                )
            else:
                success = meta.get("success", 0)
                failure = meta.get("failure", 0)
                total = success + failure
                local_rate = (success / total) if total > 0 else None
                try:
                    maybe_adopt_community_recipe(
                        self, operation, step, community,
                        local_success_rate=local_rate,
                        local_sample_count=total,
                    )
                except Exception:
                    logger.debug(
                        f"Community recipe adoption check raised for "
                        f"{operation}/{step}",
                        exc_info=True,
                    )

        # Record the check (whether or not we adopted) so the next
        # get() call doesn't repeat work until the cache changes again.
        self.update_meta(operation, step, {"last_community_check": cache_mtime})

    def save(
        self,
        operation: str,
        step: str,
        code: str,
        extra_meta: dict[str, Any] | None = None,
    ) -> Path:
        """Write *code* as a recipe file and reset its stats.

        *extra_meta* (optional) is merged into the sidecar meta.json so
        callers can persist fields like ``display_name``, ``vlm_prompt``,
        ``affiliated_app``, ``source``, and ``target_page``.

        Always records:
          - ``version``: sha256(code)[:12] — stable identifier of the
            recipe content, matches the community-side hash format.
          - ``source``: where the recipe came from (``self`` if not
            provided in extra_meta; ``maybe_adopt_community_recipe``
            sets ``community``).
          - ``adopted_at``: epoch seconds when this code became active.

        Before overwriting, if the existing recipe is "stable" (has at
        least one recorded success and isn't disabled) AND the new code
        differs, the existing version is copied to ``.previous.py`` /
        ``.previous.meta.json`` so a future failed adoption can roll
        back to it via ``rollback_to_previous``. Same-content saves
        (re-adopting the same hash) skip the backup step.
        """
        new_version = _content_version(code)
        py_path = self._recipe_path(operation, step)

        # Sticky fields preserved across saves — without this the meta
        # reset would forget which community versions we've already
        # rolled back from.
        sticky: dict[str, Any] = {}

        # Backup current stable version before overwrite
        if py_path.exists():
            existing_meta = self._read_meta(operation, step)
            existing_version = existing_meta.get("version", "")
            existing_denied = existing_meta.get("denied_community_versions")
            if isinstance(existing_denied, list) and existing_denied:
                sticky["denied_community_versions"] = existing_denied
            if existing_version != new_version:
                is_stable = (
                    existing_meta.get("success", 0) > 0
                    and not existing_meta.get("disabled")
                )
                if is_stable:
                    try:
                        prev_py = self._previous_path(operation, step)
                        prev_meta = self._previous_meta_path(operation, step)
                        prev_py.write_text(
                            py_path.read_text(encoding="utf-8"), encoding="utf-8"
                        )
                        prev_meta.write_text(
                            json.dumps(existing_meta, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        logger.info(
                            f"Backed up stable {operation}/{step} "
                            f"(version={existing_version}, "
                            f"source={existing_meta.get('source', 'unknown')}) "
                            f"to .previous before overwrite"
                        )
                    except OSError as e:
                        logger.warning(
                            f"Failed to backup {operation}/{step}: {e}"
                        )

        py_path.parent.mkdir(parents=True, exist_ok=True)
        py_path.write_text(code, encoding="utf-8")

        meta: dict[str, Any] = {
            "success": 0,
            "failure": 0,
            "disabled": False,
            "version": new_version,
            "source": "self",
            "adopted_at": time.time(),
        }
        meta.update(sticky)
        if extra_meta:
            meta.update(extra_meta)
        # extra_meta may carry source/version/adopted_at overrides
        # (e.g. community adoption sets source="community"); the merge
        # above honors those. But we always re-stamp version with the
        # actual content hash to keep meta and code in sync.
        meta["version"] = new_version
        self._write_meta(operation, step, meta)
        logger.info(
            f"Recipe saved: {operation}/{step} -> {py_path} "
            f"(version={meta['version']}, source={meta.get('source')})"
        )
        return py_path

    def update_meta(
        self, operation: str, step: str, updates: dict[str, Any]
    ) -> None:
        """Merge *updates* into existing meta without touching stats."""
        meta = self._read_meta(operation, step)
        meta.update(updates)
        self._write_meta(operation, step, meta)

    def rollback_to_previous(self, operation: str, step: str) -> bool:
        """Restore the .previous.* slot into the active slot.

        Used when a freshly-adopted (typically community-sourced) recipe
        proves to fail in this device's context — we revert to the last
        known-stable version we backed up via ``save()``.

        Side effects:
          - Active code + meta replaced with the previous version's
            (preserving its old success/failure stats so the
            restoration is "as it was").
          - The previously-active version's ``community_version`` (if
            any) is appended to the restored ``denied_community_versions``
            list, so ``_maybe_sync_with_community`` will skip re-
            adopting the same bad version on the next sync. List is
            capped at the most recent ``_DENIED_LIST_MAX`` entries.
          - ``last_community_check`` is reset to 0 so the next ``get()``
            re-checks the cache (a NEW community version may still be
            adoptable).
          - The .previous slot is consumed (deleted) — only one rollback
            target is kept at a time.

        Returns True if rollback occurred, False if no .previous slot
        existed.
        """
        prev_py = self._previous_path(operation, step)
        prev_meta_path = self._previous_meta_path(operation, step)
        if not prev_py.exists() or not prev_meta_path.exists():
            return False

        # Capture the bad version's community_version (if it had one)
        # so we can deny it going forward.
        current_meta = self._read_meta(operation, step)
        bad_community_version = current_meta.get("community_version", "")
        bad_version_hash = current_meta.get("version", "")
        bad_source = current_meta.get("source", "unknown")

        try:
            prev_code = prev_py.read_text(encoding="utf-8")
            restored_meta_raw = prev_meta_path.read_text(encoding="utf-8")
            restored_meta: dict[str, Any] = json.loads(restored_meta_raw)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                f"Cannot rollback {operation}/{step}: failed to read .previous "
                f"({e})"
            )
            return False

        # Carry over the denied list and append the bad version.
        denied = list(restored_meta.get("denied_community_versions", []))
        if bad_community_version and bad_community_version not in denied:
            denied.append(bad_community_version)
        restored_meta["denied_community_versions"] = denied[-_DENIED_LIST_MAX:]
        # Force re-consult on next get() — a NEW community version
        # might be available and still wanted.
        restored_meta["last_community_check"] = 0
        # Defensive: the restored version was working before; clear any
        # stale disabled flag.
        restored_meta["disabled"] = False
        # Tag this restoration so meta carries forensics about what
        # we rolled back from.
        restored_meta["rolled_back_from"] = {
            "version": bad_version_hash,
            "source": bad_source,
            "community_version": bad_community_version,
            "at": time.time(),
        }

        py_path = self._recipe_path(operation, step)
        py_path.write_text(prev_code, encoding="utf-8")
        self._write_meta(operation, step, restored_meta)

        # Consume .previous now that it's the active version
        try:
            prev_py.unlink()
            prev_meta_path.unlink()
        except OSError:
            pass

        logger.warning(
            f"Rolled back {operation}/{step}: "
            f"disabled version={bad_version_hash} "
            f"(source={bad_source}, community_version={bad_community_version!r}); "
            f"restored version={restored_meta.get('version')} "
            f"(source={restored_meta.get('source')})"
        )
        try:
            from openclaw_agent.engine.learning.event_log import emit
            emit("recipe.rolled_back", {
                "from_version": bad_version_hash,
                "from_source": bad_source,
                "from_community_version": bad_community_version,
                "to_version": restored_meta.get("version"),
                "to_source": restored_meta.get("source"),
            }, operation=operation, step=step)
        except Exception:
            pass
        return True

    def record_result(
        self,
        operation: str,
        step: str,
        success: bool,
        duration_ms: float | None = None,
        vlm_fallback: bool = False,
        variant: str = "current",
    ) -> None:
        """Record an execution result with extended metrics.

        *variant* selects which meta file to update: ``"current"`` (default)
        writes to the normal ``.meta.json``; ``"candidate"`` writes to
        ``.candidate.meta.json``.
        """
        meta = self._read_meta(operation, step, variant=variant)
        key = "success" if success else "failure"
        meta[key] = meta.get(key, 0) + 1
        meta["total_runs"] = meta.get("total_runs", 0) + 1

        # Streak: consecutive successes (reset on failure)
        if success:
            meta["streak"] = meta.get("streak", 0) + 1
        else:
            meta["streak"] = 0

        # Timing
        import time as _time
        meta["last_run_ts"] = _time.time()
        if duration_ms is not None:
            meta["last_duration_ms"] = duration_ms
            prev_avg = meta.get("avg_duration_ms", 0)
            n = meta.get("total_runs", 1)
            meta["avg_duration_ms"] = round(
                prev_avg + (duration_ms - prev_avg) / n, 1)

        # VLM fallback tracking
        if vlm_fallback:
            meta["vlm_fallback_count"] = meta.get("vlm_fallback_count", 0) + 1

        total = meta.get("success", 0) + meta.get("failure", 0)
        just_disabled = False
        if total >= _MIN_SAMPLES:
            fail_rate = meta.get("failure", 0) / total
            if fail_rate > _DISABLE_THRESHOLD and not meta.get("disabled"):
                meta["disabled"] = True
                just_disabled = True
                logger.warning(
                    f"Recipe {operation}/{step} auto-disabled "
                    f"(fail {fail_rate * 100:.0f}%, n={total})"
                )
                try:
                    from openclaw_agent.engine.learning.event_log import emit
                    emit("recipe.disabled", {
                        "failure_rate": round(fail_rate, 3),
                        "total_runs": total,
                    }, operation=operation, step=step)
                except Exception:
                    pass

        self._write_meta(operation, step, meta, variant=variant)

        # Auto-rollback hook: only on the active variant, only when we
        # *just* disabled, and only when a backup is available. The
        # rollback overwrites meta with the restored version, so it must
        # run AFTER the disabled-meta write above (otherwise we'd lose
        # the rollback bookkeeping if rollback rewrites meta first).
        if just_disabled and variant == "current":
            if self._previous_path(operation, step).exists():
                self.rollback_to_previous(operation, step)

    # -- candidate recipe management ----------------------------------------

    def save_candidate(
        self,
        operation: str,
        step: str,
        code: str,
        reason: str = "",
        extra_meta: dict[str, Any] | None = None,
    ) -> Path:
        """Write *code* as a candidate recipe (``.candidate.py``)."""
        py_path = self._candidate_path(operation, step)
        py_path.parent.mkdir(parents=True, exist_ok=True)
        py_path.write_text(code, encoding="utf-8")

        meta: dict[str, Any] = {
            "success": 0, "failure": 0, "disabled": False,
            "total_runs": 0, "streak": 0,
            "vlm_fallback_count": 0,
            "reason": reason,
        }
        if extra_meta:
            meta.update(extra_meta)
        self._write_meta(operation, step, meta, variant="candidate")
        logger.info(f"Candidate saved: {operation}/{step} -> {py_path}")
        return py_path

    def get_candidate(self, operation: str, step: str) -> RecipeModule | None:
        """Load the candidate recipe if it exists."""
        py_path = self._candidate_path(operation, step)
        if not py_path.exists():
            return None
        meta = self._read_meta(operation, step, variant="candidate")
        if meta.get("disabled"):
            return None
        try:
            spec = importlib.util.spec_from_file_location(
                f"recipe.{operation}.{step}.candidate", py_path,
            )
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "execute"):
                return None
            return RecipeModule(py_path, mod)
        except Exception:
            logger.exception(f"Failed to load candidate {py_path}")
            return None

    def has_candidate(self, operation: str, step: str) -> bool:
        return self._candidate_path(operation, step).exists()

    def promote(self, operation: str, step: str) -> bool:
        """Promote candidate to current, backing up the old current."""
        cand_py = self._candidate_path(operation, step)
        cand_meta = self._candidate_meta_path(operation, step)
        if not cand_py.exists():
            logger.warning(f"No candidate to promote: {operation}/{step}")
            return False

        cur_py = self._recipe_path(operation, step)
        cur_meta = self._meta_path(operation, step)

        # Back up old current
        if cur_py.exists():
            backup = cur_py.with_suffix(".prev.py")
            backup.write_text(cur_py.read_text(encoding="utf-8"),
                              encoding="utf-8")
        if cur_meta.exists():
            backup_m = cur_meta.with_name(
                cur_meta.name.replace(".meta.json", ".prev.meta.json"))
            backup_m.write_text(cur_meta.read_text(encoding="utf-8"),
                                encoding="utf-8")

        # Move candidate → current (reset run stats, keep metadata)
        cur_py.write_text(cand_py.read_text(encoding="utf-8"),
                          encoding="utf-8")
        cm = self._read_meta(operation, step, variant="candidate")
        cm["success"] = 0
        cm["failure"] = 0
        cm["total_runs"] = 0
        cm["streak"] = 0
        cm["vlm_fallback_count"] = 0
        cm.pop("reason", None)
        self._write_meta(operation, step, cm)

        cand_py.unlink()
        if cand_meta.exists():
            cand_meta.unlink()

        logger.info(f"Promoted candidate -> current: {operation}/{step}")
        return True

    def rollback(self, operation: str, step: str, reason: str = "") -> bool:
        """Delete candidate, keep current unchanged."""
        cand_py = self._candidate_path(operation, step)
        cand_meta = self._candidate_meta_path(operation, step)
        removed = False
        if cand_py.exists():
            cand_py.unlink()
            removed = True
        if cand_meta.exists():
            cand_meta.unlink()
            removed = True
        if removed:
            logger.info(
                f"Rolled back candidate: {operation}/{step}"
                f" reason={reason}")
        return removed

    def _candidate_path(self, operation: str, step: str) -> Path:
        return self._dir / operation / f"{step}.candidate.py"

    def _candidate_meta_path(self, operation: str, step: str) -> Path:
        return self._dir / operation / f"{step}.candidate.meta.json"

    def disable(self, operation: str, step: str) -> None:
        meta = self._read_meta(operation, step)
        meta["disabled"] = True
        self._write_meta(operation, step, meta)

    def enable(self, operation: str, step: str) -> None:
        meta = self._read_meta(operation, step)
        meta["disabled"] = False
        meta["success"] = 0
        meta["failure"] = 0
        self._write_meta(operation, step, meta)

    def list_recipes(self, operation: str | None = None) -> list[dict[str, Any]]:
        """List all recipes, optionally filtered by operation."""
        results: list[dict[str, Any]] = []
        search_dir = self._dir / operation if operation else self._dir
        if not search_dir.exists():
            return results
        for py in sorted(search_dir.rglob("*.py")):
            if ".candidate." in py.name or ".prev." in py.name:
                continue
            step = py.stem
            op = str(py.parent.relative_to(self._dir))
            meta = self._read_meta(op, step)
            has_cand = self.has_candidate(op, step)
            if has_cand:
                meta["has_candidate"] = True
                meta["candidate_meta"] = self._read_meta(
                    op, step, variant="candidate")
            results.append({"operation": op, "step": step, **meta})
        return results

    def read_meta(
        self, operation: str, step: str, variant: str = "current",
    ) -> dict[str, Any]:
        """Public accessor for the sidecar meta dict."""
        return self._read_meta(operation, step, variant=variant)

    # -- internal helpers --------------------------------------------------

    def _read_meta(
        self, operation: str, step: str, variant: str = "current",
    ) -> dict[str, Any]:
        path = (self._candidate_meta_path(operation, step)
                if variant == "candidate"
                else self._meta_path(operation, step))
        if not path.exists():
            return {"success": 0, "failure": 0, "disabled": False}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"success": 0, "failure": 0, "disabled": False}
        # Migration: old format may have success/failure without total_runs
        if "total_runs" not in data:
            data["total_runs"] = data.get("success", 0) + data.get("failure", 0)
        if "vlm_fallback_count" not in data:
            data["vlm_fallback_count"] = 0
        # Ensure new schema fields have defaults
        data.setdefault("frozen", False)
        data.setdefault("target_page", None)
        data.setdefault("verify_type", None)
        data.setdefault("description", "")
        return data

    def _write_meta(
        self, operation: str, step: str, meta: dict,
        variant: str = "current",
    ) -> None:
        path = (self._candidate_meta_path(operation, step)
                if variant == "candidate"
                else self._meta_path(operation, step))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                        encoding="utf-8")
