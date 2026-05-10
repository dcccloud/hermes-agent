"""MCP server endpoint for the community side.

Exposes ``nurture.recipes.get`` / ``nurture.graph.get`` /
``nurture.event.query`` as MCP tools so device-side Pi Agents can
auto-discover them via ``register_mcp_servers``. See
``docs/avatar-hermes/mcp-http-protocol.md`` §3.

This module builds the MCP server. Mounting under FastAPI is done in
``__init__.py`` (Streamable HTTP transport via ``FastMCP.streamable_http_app``).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .knowledge_engine import KnowledgeEngine

logger = logging.getLogger(__name__)


try:
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-untyped]
    _MCP_AVAILABLE = True
except ImportError:
    FastMCP = None  # type: ignore[assignment,misc]
    _MCP_AVAILABLE = False


def create_mcp_server(engine: KnowledgeEngine) -> Any:
    """Build a FastMCP server bound to the knowledge engine.

    Note: tool-level auth (avatarId-scoped, scope=admin checks) is
    enforced by the Streamable-HTTP middleware mounted in __init__.py;
    the tool implementations here can assume the caller is already
    authenticated. (For Phase 3 MVP we trust the FastAPI HTTP layer
    middleware to gate /mcp via the same Bearer token used elsewhere.)
    """
    if not _MCP_AVAILABLE:
        raise ImportError(
            "MCP server requires the 'mcp' package. "
            "pip install mcp"
        )

    # streamable_http_path="/" so when this app is mounted under "/mcp"
    # in the parent FastAPI, the public endpoint is `/mcp` (not the
    # default `/mcp/mcp` from path doubling).
    mcp = FastMCP(
        "nurture-community",
        streamable_http_path="/",
        instructions=(
            "Avatar-Hermes community knowledge engine. Query recipes, "
            "graph states, and events from the community of devices. "
            "All queries are read-only. For task creation / directive "
            "creation (admin-only), use the REST API."
        ),
    )

    @mcp.tool()
    async def recipes_get(app: str, operation: str, step: str = "main") -> str:
        """Get the best recipe for an app/operation/step from the community.

        Args:
            app: App name (e.g. 'douyin', 'facebook').
            operation: Operation id (e.g. 'enter_data_center').
            step: Recipe step name. Default 'main'.

        Returns:
            JSON object with code/version/globalSuccessRate/originNodeId, or
            null if no recipe is available.
        """
        best = await engine.get_best_recipe(app, operation, step)
        if best is None:
            return json.dumps(None)
        return json.dumps({
            "code": best.code,
            "version": best.version,
            "globalSuccessRate": best.globalSuccessRate,
            "globalSampleCount": best.globalSampleCount,
            "originNodeId": best.originNodeId,
            "originDeviceModel": best.originDeviceModel,
        }, ensure_ascii=False)

    @mcp.tool()
    async def recipes_list(app: str, operation: str, step: str = "main") -> str:
        """List ALL known recipe versions for an app/operation/step.

        Used by the community to inspect candidates before fusion. For
        execution use ``recipes_get`` (best version only).
        """
        versions = await engine.get_recipe_versions(app, operation, step)
        return json.dumps([
            {
                "version": v.version,
                "originNodeId": v.originNodeId,
                "originDeviceModel": v.originDeviceModel,
                "globalSuccessRate": v.globalSuccessRate,
                "globalSampleCount": v.globalSampleCount,
                "createdAt": v.createdAt,
            } for v in versions
        ], ensure_ascii=False)

    @mcp.tool()
    async def graph_get(app: str, consensus_only: bool = True) -> str:
        """Get the consensus app graph (page states + transitions).

        Args:
            app: App name.
            consensus_only: When True (default), only return states confirmed
                by >= 2 nodes. Set False to see all reported states.
        """
        if consensus_only:
            states = await engine.get_consensus_graph_states(app)
        else:
            states = await engine.get_graph_states(app)
        return json.dumps(
            {"states": [s.to_dict() for s in states]},
            ensure_ascii=False,
        )

    @mcp.tool()
    async def event_query(
        event_type: Optional[str] = None,
        operation: Optional[str] = None,
        limit: int = 100,
    ) -> str:
        """Query recent events from the community.

        Most useful for analytics. Args mirror the EventStore filter API.
        """
        if event_type:
            events = await engine.get_events_by_type(event_type)
        else:
            events = await engine.get_recent_events(limit=limit)
        if operation:
            events = [e for e in events if e.operation == operation]
        events = events[:limit]
        return json.dumps(
            [e.to_dict() for e in events],
            ensure_ascii=False,
        )

    return mcp
