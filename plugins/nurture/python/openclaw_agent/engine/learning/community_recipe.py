"""Community recipe consultation — pull-based recipe sharing.

Before generating or optimizing a recipe via LLM, the agent checks a
local cache of community best-recipes (written by the TypeScript
community connector after each capability update).  If a community
recipe is good enough, it is adopted directly, saving an LLM call.

Cache file: ``NURTURE_WORKSPACE/agent/community-recipes-cache.json``
"""

import json
import os
from pathlib import Path
from typing import Any

from openclaw_agent.engine.common.logger import get_logger
from openclaw_agent.engine.learning.recipe_generator import _validate_code
from openclaw_agent.engine.learning.recipe_store import RecipeStore

logger = get_logger("community_recipe")

# Min community sample count to consider adoption
_MIN_COMMUNITY_SAMPLES = 5
# Community success rate threshold when local has no recipe
_ADOPT_RATE_NO_LOCAL = 0.7
# Strong community recipe — adopt even if local is decent
_ADOPT_RATE_STRONG = 0.9
# Margin by which community must exceed local to adopt
_ADOPT_MARGIN = 0.05


def _resolve_cache_path() -> Path:
    workspace = os.environ.get(
        "NURTURE_WORKSPACE",
        str(Path(__file__).resolve().parent.parent.parent.parent.parent / "nurture-workspace"),
    )
    return Path(workspace) / "agent" / "community-recipes-cache.json"


def get_community_best_recipe(
    app: str, operation: str, step: str,
) -> dict[str, Any] | None:
    """Read the local community recipe cache and return the best match.

    Returns ``None`` if no matching recipe is found or the cache is
    missing/stale.
    """
    cache_path = _resolve_cache_path()
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    recipes = data.get("recipes", [])
    for r in recipes:
        if (
            r.get("app") == app
            and r.get("operation") == operation
            and r.get("step") == step
            and r.get("code")
        ):
            return r
    return None


def should_adopt_community_recipe(
    community: dict[str, Any],
    local_success_rate: float | None = None,
    local_sample_count: int = 0,
) -> bool:
    """Decide whether to adopt a community recipe over local generation.

    Decision table:
    - Community samples < 5           → no (insufficient data)
    - Community has no code           → no
    - Local has no recipe + rate ≥ 0.7 → yes
    - Community rate ≥ 0.9 (strong)   → yes
    - Community rate > local + 0.05   → yes
    - Otherwise                       → no
    """
    global_samples = community.get("globalSampleCount", 0)
    if global_samples < _MIN_COMMUNITY_SAMPLES:
        return False

    if not community.get("code"):
        return False

    global_rate = community.get("globalSuccessRate", 0)

    # No local recipe — adopt if community is decent
    if local_success_rate is None or local_sample_count == 0:
        return global_rate >= _ADOPT_RATE_NO_LOCAL

    # Strong community recipe
    if global_rate >= _ADOPT_RATE_STRONG:
        return True

    # Community meaningfully better than local
    if global_rate > local_success_rate + _ADOPT_MARGIN:
        return True

    return False


def maybe_adopt_community_recipe(
    recipe_store: RecipeStore,
    operation: str,
    step: str,
    community: dict[str, Any],
    local_success_rate: float | None = None,
    local_sample_count: int = 0,
) -> bool:
    """Check community recipe and adopt it if beneficial.

    Validates the code before saving. Returns True if a community
    recipe was adopted, False otherwise.
    """
    if not should_adopt_community_recipe(
        community, local_success_rate, local_sample_count
    ):
        return False

    code = community.get("code", "")
    if not code:
        return False

    # Validate before importing
    valid, reason = _validate_code(code)
    if not valid:
        logger.warning(
            f"Community recipe for {operation}/{step} failed validation: {reason}"
        )
        return False

    # Save as current recipe
    from openclaw_agent.engine.learning.recipe_generator import _extract_recipe_meta

    extra = _extract_recipe_meta(code)
    extra["source"] = "community"
    extra["community_version"] = community.get("version", "")
    extra["community_success_rate"] = community.get("globalSuccessRate", 0)
    extra["community_sample_count"] = community.get("globalSampleCount", 0)
    extra["community_origin_node"] = community.get("originNodeId", "")

    recipe_store.save(operation, step, code, extra_meta=extra)
    logger.info(
        f"Adopted community recipe for {operation}/{step} "
        f"(rate={community.get('globalSuccessRate', 0):.0%}, "
        f"samples={community.get('globalSampleCount', 0)}, "
        f"origin={community.get('originNodeId', '')})"
    )
    try:
        from openclaw_agent.engine.learning.event_log import emit
        emit("recipe.community_adopted", {
            "global_success_rate": community.get("globalSuccessRate", 0),
            "global_sample_count": community.get("globalSampleCount", 0),
            "origin_node": community.get("originNodeId", ""),
        }, operation=operation, step=step)
    except Exception:
        pass
    return True
