"""Distribution — adaptive recipe and graph push to agent nodes.

Determines which avatars should receive recipe updates (based on
performance gaps) and distributes consensus-verified graph pages to
nodes that haven't seen them yet.

In the Avatar-Hermes architecture, the actual "send" is done by the
caller (which knows how to route to MCP / REST). This module produces
the targeting decisions and event payloads.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .stores.graph_consensus import GraphConsensus
from .stores.recipe_store import RecipeEntry, RecipeStore

logger = logging.getLogger(__name__)


# Same legacy import rewrites as the recipe-store; applied once more
# at distribution time to be defensive — old data may be in the store.
_IMPORT_REWRITES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\boperation_engine\.learning\.humanize\b"),
     "openclaw_agent.engine.learning.humanize"),
]


def _sanitize_recipe_code(code: str) -> str:
    for pattern, replacement in _IMPORT_REWRITES:
        code = pattern.sub(replacement, code)
    return code


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class DistributionTarget:
    nodeId: str
    reason: str


@dataclass
class RecipePushPayload:
    nodeId: str
    event: str
    payload: Dict[str, Any]


@dataclass
class GraphPushPayload:
    nodeId: str
    event: str
    payload: Dict[str, Any]


# ---------------------------------------------------------------------------
# Recipe distribution
# ---------------------------------------------------------------------------


def get_distribution_targets(
    store: RecipeStore,
    app: str,
    operation: str,
    step: str,
    connected_node_ids: List[str],
    *,
    threshold: float = 0.1,
    min_rate: float = 0.7,
) -> List[DistributionTarget]:
    """Find nodes that should receive a recipe update.

    A node qualifies if:
      - its local success_rate < globalAvg - threshold
      - OR it has no recipe at all for this step
      - AND the canonical recipe has globalSuccessRate >= min_rate
    """
    best = store.get_best_recipe(app, operation, step)
    if best is None or not best.code:
        return []
    if best.globalSuccessRate < min_rate:
        return []

    targets: List[DistributionTarget] = []
    global_avg = store.get_global_success_rate(app, operation, step)

    for node_id in connected_node_ids:
        node_stats = best.nodeStats.get(node_id)
        if node_stats is None:
            targets.append(DistributionTarget(nodeId=node_id, reason="no_recipe"))
            continue
        node_total = node_stats.success + node_stats.failure
        if node_total == 0:
            continue
        node_rate = node_stats.success / node_total
        if node_rate < global_avg - threshold:
            targets.append(DistributionTarget(
                nodeId=node_id,
                reason=(
                    f"local_rate={node_rate * 100:.1f}% < "
                    f"global_avg={global_avg * 100:.1f}% - "
                    f"{threshold * 100:.0f}%"
                ),
            ))
    return targets


def build_recipe_push_payloads(
    store: RecipeStore,
    connected_node_ids: List[str],
) -> List[RecipePushPayload]:
    """Determine targeting + payloads for recipe pushes.

    Returns a list of ``RecipePushPayload`` ready to be sent by the
    caller (community server / MCP / WebHook). No I/O happens here —
    pure logic.
    """
    if not connected_node_ids:
        return []

    payloads: List[RecipePushPayload] = []
    for cand in store.get_fusion_candidates():
        app = cand["app"]
        operation = cand["operation"]
        step = cand["step"]
        best = store.get_best_recipe(app, operation, step)
        if best is None or not best.code:
            continue
        if best.globalSuccessRate < 0.7:
            continue

        targets = get_distribution_targets(
            store, app, operation, step, connected_node_ids,
        )
        for t in targets:
            payloads.append(RecipePushPayload(
                nodeId=t.nodeId,
                event="nurture.recipe.push",
                payload={
                    "app": app,
                    "operation": operation,
                    "step": step,
                    "code": _sanitize_recipe_code(best.code),
                    "version": best.version,
                    "origin": best.originNodeId,
                    "global_success_rate": best.globalSuccessRate,
                },
            ))
    return payloads


# ---------------------------------------------------------------------------
# Graph distribution
# ---------------------------------------------------------------------------


def build_graph_push_payloads(
    graph_consensus: GraphConsensus,
    connected_node_ids: List[str],
    source_node_id: str,
) -> List[GraphPushPayload]:
    """Build push payloads for consensus-verified graph pages.

    Pages are pushed to all *other* connected nodes that haven't yet
    confirmed each page.
    """
    if len(connected_node_ids) < 2:
        return []

    payloads: List[GraphPushPayload] = []
    for app in graph_consensus.get_apps():
        consensus_pages = graph_consensus.get_consensus_states(app)
        if not consensus_pages:
            continue

        for target_node_id in connected_node_ids:
            if target_node_id == source_node_id:
                continue
            pages = [
                {
                    "state_id": p.stateId,
                    "name": p.name,
                    "description": p.description,
                    "indicators": p.indicators,
                    "discovery_path": p.discoveryPath,
                    "is_optional": p.isOptional,
                    "transitions": p.transitions,
                }
                for p in consensus_pages
                if target_node_id not in p.confirmedByNodes
            ]
            if pages:
                payloads.append(GraphPushPayload(
                    nodeId=target_node_id,
                    event="nurture.graph.push",
                    payload={"app": app, "pages": pages},
                ))
    return payloads
