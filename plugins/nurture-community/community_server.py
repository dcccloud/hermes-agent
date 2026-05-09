"""FastAPI server for the community side of Avatar-Hermes.

Implements the REST endpoints in ``mcp-http-protocol.md`` §4. The MCP
server endpoint mounts at ``/mcp`` and is built in ``mcp_server.py``.

Endpoints (all REST, JWT Bearer auth):
  POST /api/avatar/register      — first-time avatar registration
  POST /api/upload               — capability / trace / event ingest (kind-routed)
  GET  /api/tasks/poll           — self-scoped active task list
  GET  /api/advices/poll         — self-scoped advice queue
  GET  /api/directives/poll      — self-scoped directive queue
  POST /api/task/complete        — outcome reporting
  GET  /api/health               — unauthenticated probe
  GET  /api/recipes/{app}/{op}/{step}  — best recipe (also via MCP)
  GET  /api/graph/{app}                 — consensus graph (also via MCP)

Self-scoped contract: ``avatarId`` is ALWAYS taken from the verified
JWT payload, never from the request body. See ``boundary-contracts.md``
不变量 5.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

import jwt
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .auth import JwtAuth, TokenPayload
from .avatar_registry import AvatarCapability, AvatarRegistry
from .knowledge_engine import KnowledgeEngine
from .stores._helpers import now_ms
from .stores.task_dispatch import TaskRequirements

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    deviceId: str
    agentId: str
    publicKey: str = ""
    platform: str = "android"
    deviceModel: str = ""
    displayName: str = ""


class RegisterResponse(BaseModel):
    avatarId: str
    token: str
    expiresAt: int


class UploadRequest(BaseModel):
    kind: str  # "capability" | "trace" | "event"
    agentId: str = ""
    deviceId: str = ""
    timestamp: float = 0.0
    payload: Dict[str, Any] = Field(default_factory=dict)


class TaskCompleteRequest(BaseModel):
    taskId: str
    outcome: str  # success | failed | refused | partial
    reason: str = ""
    summary: Optional[str] = None


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    knowledge_engine: KnowledgeEngine,
    auth: JwtAuth,
    avatar_registry: AvatarRegistry,
) -> FastAPI:
    """Build the FastAPI app. The caller is responsible for running it
    via uvicorn (see ``__init__.py``).
    """
    app = FastAPI(
        title="Avatar-Hermes Community",
        description=(
            "REST endpoints for device-community sync. See "
            "docs/avatar-hermes/mcp-http-protocol.md for the protocol."
        ),
    )

    def _verify_token(authorization: str) -> TokenPayload:
        """Extract Bearer token from the Authorization header and verify it.

        Raises HTTPException(401) on missing/malformed/expired tokens.
        """
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization Bearer token required",
            )
        token = authorization.split(None, 1)[1].strip()
        try:
            return auth.verify(token)
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired",
            )
        except jwt.InvalidTokenError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {e}",
            )

    def _require_admin(token: TokenPayload) -> None:
        if token.scope != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin scope required",
            )

    # -- health check (unauthenticated) -------------------------------------

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "backend": "postgres" if knowledge_engine.is_pg else "json",
            "timestamp": now_ms(),
        }

    # -- avatar registration ------------------------------------------------

    @app.post("/api/avatar/register", response_model=RegisterResponse)
    async def avatar_register(req: RegisterRequest) -> RegisterResponse:
        """First-time registration. Returns a 30-day Bearer token."""
        avatar_id = f"avatar-{uuid.uuid4().hex[:12]}"
        avatar_registry.register(
            avatar_id=avatar_id,
            device_model=req.deviceModel or req.deviceId,
            display_name=req.displayName or req.agentId or avatar_id,
            platform=req.platform,  # type: ignore[arg-type]
        )
        token = auth.issue(
            avatar_id=avatar_id,
            agent_id=req.agentId,
            scope="agent",
        )
        decoded = auth.verify(token)
        logger.info(
            "avatar registered: %s (agentId=%s, device=%s)",
            avatar_id, req.agentId, req.deviceId,
        )
        return RegisterResponse(
            avatarId=avatar_id,
            token=token,
            expiresAt=decoded.expires_at,
        )

    # -- unified upload (capability / trace / event) ------------------------

    @app.post("/api/upload")
    async def upload(
        req: UploadRequest,
        authorization: str = Header(None),
    ) -> Dict[str, Any]:
        token = _verify_token(authorization)
        avatar_id = token.avatar_id
        avatar_registry.heartbeat(avatar_id)

        if req.kind == "capability":
            await knowledge_engine.ingest_capability_update(
                avatar_id, req.payload.get("device_model", ""),
                req.payload,
            )
            # Update avatar capability index from the payload
            apps_dict = req.payload.get("apps", {}) or {}
            caps: List[AvatarCapability] = []
            for app_name, app_data in apps_dict.items():
                if not isinstance(app_data, dict):
                    continue
                ops = [op.get("name", "") for op in app_data.get("operations", [])
                       if isinstance(op, dict) and op.get("name")]
                has_recipe = any(
                    s.get("has_recipe") for op in app_data.get("operations", [])
                    if isinstance(op, dict)
                    for s in (op.get("steps") or [])
                    if isinstance(s, dict)
                )
                caps.append(AvatarCapability(
                    app=app_name, operations=ops, hasRecipe=has_recipe,
                ))
            avatar_registry.update_capabilities(avatar_id, caps)

            # Build best-recipes response
            best_recipes: List[Dict[str, Any]] = []
            for cap in caps:
                for op_name in cap.operations:
                    best = await knowledge_engine.get_best_recipe(
                        cap.app, op_name, "main",
                    )
                    if best is not None and best.code:
                        best_recipes.append({
                            "app": best.app,
                            "operation": best.operation,
                            "step": best.step,
                            "code": best.code,
                            "version": best.version,
                            "globalSuccessRate": best.globalSuccessRate,
                            "globalSampleCount": best.globalSampleCount,
                            "originNodeId": best.originNodeId,
                            "originDeviceModel": best.originDeviceModel,
                        })
            return {"ok": True, "bestRecipes": best_recipes}

        if req.kind == "trace":
            traces = req.payload.get("traces") or []
            ingested = 0
            for t in traces:
                if isinstance(t, dict):
                    await knowledge_engine.ingest_trace(t)
                    ingested += 1
            return {"ok": True, "ingested": ingested}

        if req.kind == "event":
            events = req.payload.get("events") or []
            ingested = 0
            for e in events:
                if isinstance(e, dict):
                    await knowledge_engine.ingest_event(e)
                    ingested += 1
            return {"ok": True, "ingested": ingested}

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown kind: {req.kind!r}",
        )

    # -- self-scoped polling endpoints --------------------------------------

    @app.get("/api/tasks/poll")
    async def tasks_poll(authorization: str = Header(None)) -> Dict[str, Any]:
        token = _verify_token(authorization)
        avatar = avatar_registry.get(token.avatar_id)
        platform = avatar.platform if avatar else None
        all_caps: List[str] = []
        if avatar:
            for c in avatar.capabilities:
                for op in c.operations:
                    all_caps.append(f"{c.app}.{op}")
        tasks = await knowledge_engine.get_available_tasks(
            platform=platform, capabilities=all_caps,
        )
        return {
            "tasks": [t.to_dict() for t in tasks],
            "etag": str(now_ms()),
        }

    @app.get("/api/advices/poll")
    async def advices_poll(
        authorization: str = Header(None),
        app: Optional[str] = None,
    ) -> Dict[str, Any]:
        token = _verify_token(authorization)
        advices = await knowledge_engine.get_pending_advice(token.avatar_id, app)
        return {
            "advices": [a.to_dict() for a in advices],
            "etag": str(now_ms()),
        }

    @app.get("/api/directives/poll")
    async def directives_poll(
        authorization: str = Header(None),
        app: Optional[str] = None,
    ) -> Dict[str, Any]:
        token = _verify_token(authorization)
        directives = await knowledge_engine.get_pending_directives(token.avatar_id, app)
        return {
            "directives": [d.to_dict() for d in directives],
            "etag": str(now_ms()),
        }

    @app.post("/api/task/complete")
    async def task_complete(
        req: TaskCompleteRequest,
        authorization: str = Header(None),
    ) -> Dict[str, Any]:
        token = _verify_token(authorization)
        if req.outcome == "success":
            completed = await knowledge_engine.complete_task(req.taskId)
            return {
                "ok": True,
                "deactivated": completed is not None,
            }
        # Other outcomes: keep task active so other avatars can still try.
        # Just record the report (Phase 5 analytics will care).
        logger.info(
            "task report: avatarId=%s taskId=%s outcome=%s reason=%r",
            token.avatar_id, req.taskId, req.outcome, req.reason,
        )
        return {"ok": True, "deactivated": False}

    # -- read endpoints (also surfaced via MCP) -----------------------------

    @app.get("/api/recipes/{app}/{operation}/{step}")
    async def recipes_get(
        app: str,
        operation: str,
        step: str,
        authorization: str = Header(None),
    ) -> Dict[str, Any]:
        _ = _verify_token(authorization)
        best = await knowledge_engine.get_best_recipe(app, operation, step)
        if best is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No recipe for this app/operation/step",
            )
        return {
            "code": best.code,
            "version": best.version,
            "globalSuccessRate": best.globalSuccessRate,
            "globalSampleCount": best.globalSampleCount,
            "originNodeId": best.originNodeId,
            "originDeviceModel": best.originDeviceModel,
        }

    @app.get("/api/graph/{app}")
    async def graph_get(
        app: str,
        authorization: str = Header(None),
        consensus_only: bool = True,
    ) -> Dict[str, Any]:
        _ = _verify_token(authorization)
        if consensus_only:
            states = await knowledge_engine.get_consensus_graph_states(app)
        else:
            states = await knowledge_engine.get_graph_states(app)
        return {
            "states": [s.to_dict() for s in states],
        }

    return app
