"""PG-backed RecipeStore. Mirrors stores/recipe_store.py JSON API."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..stores._helpers import now_ms
from ..stores.recipe_store import (
    NodeStats,
    RecipeEntry,
    compute_code_hash,
    recipe_key,
    sanitize_recipe_code,
)


class PgRecipeStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def ingest_from_capability(
        self, node_id: str, device_model: str, apps: Dict[str, Any]
    ) -> bool:
        changed = False
        async with self._pool.acquire() as conn:
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
                        if await self._upsert_capability_step(
                            conn, node_id, device_model, app, op_name, step_info
                        ):
                            changed = True
        return changed

    async def _upsert_capability_step(
        self, conn: Any, node_id: str, device_model: str,
        app: str, op_name: str, step_info: Dict[str, Any],
    ) -> bool:
        step = step_info.get("step", "")
        success = int(step_info.get("success", 0) or 0)
        failure = int(step_info.get("failure", 0) or 0)
        new_code = step_info.get("code") or ""
        clean_code = sanitize_recipe_code(new_code) if new_code else ""

        rows = await conn.fetch(
            """
            SELECT id, version, code, origin_node_id, node_stats
            FROM nurture_recipes
            WHERE app=$1 AND operation=$2 AND step=$3
            """,
            app, op_name, step,
        )
        # Find an entry that this node already participates in
        target_id: Optional[int] = None
        target_version = ""
        target_code = ""
        for r in rows:
            stats = r["node_stats"]
            if isinstance(stats, str):
                stats = json.loads(stats)
            if r["origin_node_id"] == node_id or (stats and node_id in stats):
                target_id = r["id"]
                target_version = r["version"]
                target_code = r["code"] or ""
                break

        if target_id is not None:
            # Existing entry — update node_stats + maybe code
            new_version = target_version
            updated_code = target_code
            if clean_code and target_code != clean_code:
                updated_code = clean_code
                new_version = f"node-{node_id}-{compute_code_hash(clean_code)}"
            await conn.execute(
                """
                UPDATE nurture_recipes
                SET node_stats = jsonb_set(
                      node_stats, $1::text[],
                      $2::jsonb, true
                    ),
                    code = $3,
                    version = $4,
                    global_success_rate = (
                      SELECT (
                        SELECT COALESCE(SUM((v->>'success')::int), 0)
                        FROM jsonb_each(jsonb_set(node_stats, $1::text[], $2::jsonb, true)) AS e(k, v)
                      )::real
                      / GREATEST((
                        SELECT COALESCE(SUM(((v->>'success')::int + (v->>'failure')::int)), 0)
                        FROM jsonb_each(jsonb_set(node_stats, $1::text[], $2::jsonb, true)) AS e(k, v)
                      ), 1)
                    ),
                    global_sample_count = (
                      SELECT COALESCE(SUM(((v->>'success')::int + (v->>'failure')::int)), 0)::int
                      FROM jsonb_each(jsonb_set(node_stats, $1::text[], $2::jsonb, true)) AS e(k, v)
                    )
                WHERE id = $5
                """,
                [node_id],
                json.dumps({
                    "success": success, "failure": failure,
                    "lastReportedAt": now_ms(),
                }),
                updated_code, new_version, target_id,
            )
            return True

        # New entry
        version = (
            f"node-{node_id}-{compute_code_hash(clean_code)}"
            if clean_code else f"node-{node_id}"
        )
        await conn.execute(
            """
            INSERT INTO nurture_recipes
              (app, operation, step, version, code,
               origin_node_id, origin_device_model, created_at,
               node_stats, global_success_rate, global_sample_count)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11)
            ON CONFLICT (app, operation, step, version) DO NOTHING
            """,
            app, op_name, step, version, clean_code,
            node_id, device_model, now_ms(),
            json.dumps({node_id: {
                "success": success, "failure": failure,
                "lastReportedAt": now_ms(),
            }}),
            float(step_info.get("success_rate", 0) or 0),
            success + failure,
        )
        return True

    async def register_recipe(
        self, *, app: str, operation: str, step: str,
        code: str, node_id: str, device_model: str,
    ) -> RecipeEntry:
        clean_code = sanitize_recipe_code(code)
        version = compute_code_hash(clean_code)

        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT * FROM nurture_recipes
                WHERE app=$1 AND operation=$2 AND step=$3 AND version=$4
                """,
                app, operation, step, version,
            )
            if existing:
                return _row_to_entry(existing)

            row = await conn.fetchrow(
                """
                INSERT INTO nurture_recipes
                  (app, operation, step, version, code,
                   origin_node_id, origin_device_model, created_at,
                   node_stats, global_success_rate, global_sample_count)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'{}'::jsonb,0,0)
                ON CONFLICT (app, operation, step, version) DO UPDATE
                  SET origin_device_model = EXCLUDED.origin_device_model
                RETURNING *
                """,
                app, operation, step, version, clean_code,
                node_id, device_model, now_ms(),
            )
        return _row_to_entry(row)

    async def store_fused_recipe(
        self, *, app: str, operation: str, step: str, code: str
    ) -> RecipeEntry:
        return await self.register_recipe(
            app=app, operation=operation, step=step, code=code,
            node_id="fused", device_model="canonical",
        )

    async def get_versions(
        self, app: str, operation: str, step: str
    ) -> List[RecipeEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM nurture_recipes
                WHERE app=$1 AND operation=$2 AND step=$3
                """,
                app, operation, step,
            )
        return [_row_to_entry(r) for r in rows]

    async def get_global_success_rate(
        self, app: str, operation: str, step: str
    ) -> float:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH stats AS (
                  SELECT jsonb_each(node_stats) AS pair
                  FROM nurture_recipes
                  WHERE app=$1 AND operation=$2 AND step=$3
                )
                SELECT
                  COALESCE(SUM(((pair).value->>'success')::int), 0) AS s,
                  COALESCE(SUM(((pair).value->>'success')::int + ((pair).value->>'failure')::int), 0) AS t
                FROM stats
                """,
                app, operation, step,
            )
        if row is None or not row["t"]:
            return 0.0
        return float(row["s"]) / float(row["t"])

    async def get_best_recipe(
        self, app: str, operation: str, step: str
    ) -> Optional[RecipeEntry]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM nurture_recipes
                WHERE app=$1 AND operation=$2 AND step=$3 AND code <> ''
                ORDER BY global_success_rate DESC, global_sample_count DESC
                LIMIT 1
                """,
                app, operation, step,
            )
        return _row_to_entry(row) if row else None

    async def get_fusion_candidates(self) -> List[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH groups AS (
                  SELECT app, operation, step,
                    COUNT(*) FILTER (WHERE code <> '') AS code_versions,
                    COALESCE(SUM(global_sample_count) FILTER (WHERE code <> ''), 0) AS samples
                  FROM nurture_recipes
                  GROUP BY app, operation, step
                )
                SELECT app, operation, step
                FROM groups
                WHERE code_versions >= 2 AND samples >= 5
                """
            )
            candidates: List[Dict[str, Any]] = []
            for r in rows:
                entries = await self.get_versions(r["app"], r["operation"], r["step"])
                candidates.append({
                    "app": r["app"], "operation": r["operation"], "step": r["step"],
                    "entries": [e for e in entries if e.code],
                })
        return candidates


def _row_to_entry(row: Any) -> RecipeEntry:
    if row is None:
        raise ValueError("row is None")
    stats = row["node_stats"]
    if isinstance(stats, str):
        stats = json.loads(stats)
    return RecipeEntry(
        app=row["app"], operation=row["operation"], step=row["step"],
        version=row["version"], code=row["code"] or "",
        originNodeId=row["origin_node_id"],
        originDeviceModel=row["origin_device_model"] or "",
        createdAt=int(row["created_at"]),
        nodeStats={
            k: NodeStats.from_dict(v if isinstance(v, dict) else json.loads(v))
            for k, v in (stats or {}).items()
        },
        globalSuccessRate=float(row["global_success_rate"] or 0),
        globalSampleCount=int(row["global_sample_count"] or 0),
    )
