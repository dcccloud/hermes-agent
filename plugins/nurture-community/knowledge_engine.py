"""KnowledgeEngine — facade composing recipe store, graph consensus,
recipe fusion, distribution, traces, value schema, advice and directives
into a single community-level knowledge management interface.

Backends:
  - JSON files (default, when no databaseUrl provided)
  - PostgreSQL (when databaseUrl is set)

All public methods are ``async`` for uniform calling convention. JSON
backend methods are synchronous internally — wrapped in ``async def``
for facade consistency.

Mirrors OpenClaw's ``knowledge-engine.ts``. See ``docs/avatar-hermes/
community-memory-evolution.md``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from .stores._helpers import now_ms
from .stores.advice_engine import AdviceEngine, AdviceEntry
from .stores.directive_engine import (
    DirectiveEngine,
    DirectiveEntry,
    DirectiveInstruction,
    DirectiveType,
)
from .stores.event_store import EventEntry, EventStore
from .stores.graph_consensus import GraphConsensus, GraphStateEntry
from .stores.recipe_store import RecipeEntry, RecipeStore
from .stores.task_dispatch import (
    Priority,
    TaskDispatch,
    TaskDispatchEngine,
    TaskRequirements,
    TaskStatus,
)
from .stores.trace_store import TraceEntry, TraceStore
from .stores.value_schema import ValueSchemaCategory, ValueSchemaSnapshot, ValueSchemaStore

logger = logging.getLogger(__name__)


def _resolve_store_dir() -> Path:
    """Default knowledge dir lives under ``$NURTURE_WORKSPACE/knowledge``.

    Falls back to ``./nurture-workspace/knowledge`` if the env var isn't
    set (mirrors OpenClaw default).
    """
    workspace = os.environ.get("NURTURE_WORKSPACE") or "nurture-workspace"
    store_dir = Path(workspace) / "knowledge"
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


class KnowledgeEngine:
    """Dual-backend community knowledge facade.

    Construction:
      KnowledgeEngine()                       # JSON
      KnowledgeEngine(database_url="postgresql://...")  # PG

    For PG, call ``await engine.init()`` once at startup to run
    migration.sql.
    """

    def __init__(
        self,
        *,
        database_url: Optional[str] = None,
        store_dir: Optional[Path] = None,
    ) -> None:
        self._database_url = database_url
        self._is_pg = bool(database_url)
        self._pool: Any = None

        # ValueSchemaStore is always JSON (small, version-bumped snapshot).
        self._store_dir = Path(store_dir) if store_dir else _resolve_store_dir()
        self.value_schema = ValueSchemaStore(self._store_dir)

        if self._is_pg:
            # Lazy-import PG modules to avoid asyncpg dep at JSON-mode startup.
            from .pg.pg_advice_engine import PgAdviceEngine
            from .pg.pg_directive_engine import PgDirectiveEngine
            from .pg.pg_event_store import PgEventStore
            from .pg.pg_graph_consensus import PgGraphConsensus
            from .pg.pg_recipe_store import PgRecipeStore
            from .pg.pg_task_dispatch import PgTaskDispatchEngine
            from .pg.pg_trace_store import PgTraceStore
            # Pool created lazily in init() so __init__ stays sync-friendly.
            self._pg_classes = {
                "recipe": PgRecipeStore,
                "graph": PgGraphConsensus,
                "trace": PgTraceStore,
                "event": PgEventStore,
                "task": PgTaskDispatchEngine,
                "advice": PgAdviceEngine,
                "directive": PgDirectiveEngine,
            }
            self.recipe_store: Any = None
            self.graph_consensus: Any = None
            self.trace_store: Any = None
            self.event_store: Any = None
            self.task_dispatch: Any = None
            self.advice_engine: Any = None
            self.directive_engine: Any = None
        else:
            self.recipe_store = RecipeStore(self._store_dir)
            self.graph_consensus = GraphConsensus(self._store_dir)
            self.trace_store = TraceStore(self._store_dir)
            self.event_store = EventStore(self._store_dir)
            self.task_dispatch = TaskDispatchEngine(self._store_dir)
            self.advice_engine = AdviceEngine(self._store_dir)
            self.directive_engine = DirectiveEngine(self._store_dir)

    @property
    def is_pg(self) -> bool:
        return self._is_pg

    async def init(self) -> None:
        """Run database migration (PG only). Idempotent."""
        if not self._is_pg:
            return
        from .pg.pool import create_pool, run_migration
        if self._pool is None:
            self._pool = await create_pool(self._database_url or "")
            await run_migration(self._pool)
            self.recipe_store = self._pg_classes["recipe"](self._pool)
            self.graph_consensus = self._pg_classes["graph"](self._pool)
            self.trace_store = self._pg_classes["trace"](self._pool)
            self.event_store = self._pg_classes["event"](self._pool)
            self.task_dispatch = self._pg_classes["task"](self._pool)
            self.advice_engine = self._pg_classes["advice"](self._pool)
            self.directive_engine = self._pg_classes["directive"](self._pool)

    async def close(self) -> None:
        if self._pool is not None:
            from .pg.pool import close_pool
            await close_pool(self._pool)
            self._pool = None

    # -- Capability ingest ---------------------------------------------------

    async def ingest_capability_update(
        self,
        node_id: str,
        device_model: str,
        payload: Dict[str, Any],
    ) -> None:
        apps = payload.get("apps")
        if not isinstance(apps, dict):
            return

        if self._is_pg:
            await self.recipe_store.ingest_from_capability(node_id, device_model, apps)
        else:
            self.recipe_store.ingest_from_capability(node_id, device_model, apps)

        for app, app_data in apps.items():
            if not isinstance(app_data, dict):
                continue
            graph_summary = app_data.get("graph_summary") or {}
            verified = graph_summary.get("verified_discovered_pages") or []
            transitions_map = graph_summary.get("transitions") or {}
            for page in verified:
                if not isinstance(page, dict):
                    continue
                page_transitions = {
                    **(page.get("transitions") or {}),
                    **(transitions_map.get(page.get("state_id", "")) or {}),
                }
                await self.report_graph_state(
                    node_id,
                    app=app,
                    state_id=page.get("state_id", ""),
                    name=page.get("name", ""),
                    description=page.get("description", ""),
                    indicators=list(page.get("indicators") or []),
                    discovery_path=list(page.get("discovery_path") or []),
                    is_optional=bool(page.get("is_optional", False)),
                    verification=page.get("verification", ""),
                    transitions=page_transitions,
                )

    # -- Recipe --------------------------------------------------------------

    async def register_recipe(
        self, *, app: str, operation: str, step: str,
        code: str, node_id: str, device_model: str,
    ) -> RecipeEntry:
        kwargs = {
            "app": app, "operation": operation, "step": step,
            "code": code, "node_id": node_id, "device_model": device_model,
        }
        if self._is_pg:
            return await self.recipe_store.register_recipe(**kwargs)
        return self.recipe_store.register_recipe(**kwargs)

    async def get_recipe_versions(
        self, app: str, operation: str, step: str
    ) -> List[RecipeEntry]:
        if self._is_pg:
            return await self.recipe_store.get_versions(app, operation, step)
        return self.recipe_store.get_versions(app, operation, step)

    async def get_global_success_rate(
        self, app: str, operation: str, step: str
    ) -> float:
        if self._is_pg:
            return await self.recipe_store.get_global_success_rate(app, operation, step)
        return self.recipe_store.get_global_success_rate(app, operation, step)

    async def get_best_recipe(
        self, app: str, operation: str, step: str
    ) -> Optional[RecipeEntry]:
        if self._is_pg:
            return await self.recipe_store.get_best_recipe(app, operation, step)
        return self.recipe_store.get_best_recipe(app, operation, step)

    async def get_fusion_candidates(self) -> List[Dict[str, Any]]:
        if self._is_pg:
            return await self.recipe_store.get_fusion_candidates()
        return self.recipe_store.get_fusion_candidates()

    async def store_fused_recipe(
        self, *, app: str, operation: str, step: str, code: str
    ) -> RecipeEntry:
        kwargs = {"app": app, "operation": operation, "step": step, "code": code}
        if self._is_pg:
            return await self.recipe_store.store_fused_recipe(**kwargs)
        return self.recipe_store.store_fused_recipe(**kwargs)

    async def run_fusion(self) -> int:
        """LLM fusion across all candidate recipe groups. Returns count of
        new fused recipes stored."""
        from .recipe_fusion import fuse_recipes
        candidates = await self.get_fusion_candidates()
        if not candidates:
            return 0
        fused = 0
        for cand in candidates:
            try:
                code = await fuse_recipes(cand["entries"])
                if code:
                    await self.store_fused_recipe(
                        app=cand["app"], operation=cand["operation"],
                        step=cand["step"], code=code,
                    )
                    fused += 1
            except Exception as e:
                logger.warning("recipe fusion failed for %s: %s", cand, e)
        return fused

    # -- Graph ---------------------------------------------------------------

    async def report_graph_state(
        self, node_id: str, *, app: str, state_id: str,
        name: str, description: str,
        indicators: List[str], discovery_path: List[str],
        is_optional: bool, verification: str,
        transitions: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        kwargs = {
            "app": app, "state_id": state_id, "name": name,
            "description": description, "indicators": indicators,
            "discovery_path": discovery_path, "is_optional": is_optional,
            "verification": verification, "transitions": transitions,
        }
        if self._is_pg:
            return await self.graph_consensus.report_state(node_id, **kwargs)
        return self.graph_consensus.report_state(node_id, **kwargs)

    async def get_graph_states(self, app: str) -> List[GraphStateEntry]:
        if self._is_pg:
            return await self.graph_consensus.get_states(app)
        return self.graph_consensus.get_states(app)

    async def get_consensus_graph_states(self, app: str) -> List[GraphStateEntry]:
        if self._is_pg:
            return await self.graph_consensus.get_consensus_states(app)
        return self.graph_consensus.get_consensus_states(app)

    # -- Trace ---------------------------------------------------------------

    async def ingest_trace(self, entry: Dict[str, Any]) -> TraceEntry:
        if self._is_pg:
            return await self.trace_store.ingest(entry)
        return self.trace_store.ingest(entry)

    async def get_traces_by_app(self, app: str) -> List[TraceEntry]:
        if self._is_pg:
            return await self.trace_store.get_by_app(app)
        return self.trace_store.get_by_app(app)

    async def get_recent_traces(self, limit: int = 50) -> List[TraceEntry]:
        if self._is_pg:
            return await self.trace_store.get_recent(limit)
        return self.trace_store.get_recent(limit)

    # -- Event ---------------------------------------------------------------

    async def ingest_event(self, entry: Dict[str, Any]) -> EventEntry:
        if self._is_pg:
            return await self.event_store.ingest(entry)
        return self.event_store.ingest(entry)

    async def get_recent_events(self, limit: int = 50) -> List[EventEntry]:
        if self._is_pg:
            return await self.event_store.get_recent(limit)
        return self.event_store.get_recent(limit)

    async def get_events_by_type(self, event_type: str) -> List[EventEntry]:
        if self._is_pg:
            return await self.event_store.get_by_event_type(event_type)
        return self.event_store.get_by_event_type(event_type)

    # -- Value Schema --------------------------------------------------------

    def get_value_schema_snapshot(self) -> ValueSchemaSnapshot:
        return self.value_schema.get_snapshot()

    def update_value_schema(self, category: ValueSchemaCategory) -> None:
        self.value_schema.upsert_category(category)

    # -- Advice --------------------------------------------------------------

    async def analyze_and_generate_advice(self, app: str) -> int:
        if self._is_pg:
            return await self.advice_engine.analyze(self.trace_store, app)
        return self.advice_engine.analyze(self.trace_store, app)

    async def get_pending_advice(
        self, agent_id: str, app: Optional[str] = None
    ) -> List[AdviceEntry]:
        if self._is_pg:
            return await self.advice_engine.get_pending_for_agent(agent_id, app)
        return self.advice_engine.get_pending_for_agent(agent_id, app)

    async def mark_advice_delivered(
        self, advice_id: str, agent_id: str
    ) -> None:
        if self._is_pg:
            await self.advice_engine.mark_delivered(advice_id, agent_id)
        else:
            self.advice_engine.mark_delivered(advice_id, agent_id)

    # -- Directive -----------------------------------------------------------

    async def create_directive(
        self, *, app: str, type: DirectiveType, summary: str,
        instructions: List[DirectiveInstruction],
        priority: int = 5, expires_at: Optional[str] = None,
    ) -> DirectiveEntry:
        kwargs = {
            "app": app, "type": type, "summary": summary,
            "instructions": instructions, "priority": priority,
            "expires_at": expires_at,
        }
        if self._is_pg:
            return await self.directive_engine.create(**kwargs)
        return self.directive_engine.create(**kwargs)

    async def get_pending_directives(
        self, avatar_id: str, app: Optional[str] = None
    ) -> List[DirectiveEntry]:
        if self._is_pg:
            return await self.directive_engine.get_pending_for_avatar(avatar_id, app)
        return self.directive_engine.get_pending_for_avatar(avatar_id, app)

    async def mark_directive_delivered(
        self, directive_id: str, avatar_id: str
    ) -> None:
        if self._is_pg:
            await self.directive_engine.mark_delivered(directive_id, avatar_id)
        else:
            self.directive_engine.mark_delivered(directive_id, avatar_id)

    # -- Task ----------------------------------------------------------------

    async def create_task(
        self, *, requirements: TaskRequirements, app: str,
        description: str, priority: Priority = "normal",
        expires_at: Optional[str] = None,
    ) -> TaskDispatch:
        kwargs = {
            "requirements": requirements, "app": app,
            "description": description, "priority": priority,
            "expires_at": expires_at,
        }
        if self._is_pg:
            return await self.task_dispatch.create(**kwargs)
        return self.task_dispatch.create(**kwargs)

    async def get_available_tasks(
        self, *, platform: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
    ) -> List[TaskDispatch]:
        if self._is_pg:
            return await self.task_dispatch.get_available(
                platform=platform, capabilities=capabilities
            )
        return self.task_dispatch.get_available(
            platform=platform, capabilities=capabilities
        )

    async def complete_task(self, task_id: str) -> Optional[TaskDispatch]:
        if self._is_pg:
            return await self.task_dispatch.complete(task_id)
        return self.task_dispatch.complete(task_id)
