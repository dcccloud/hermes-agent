"""PG-backed GraphConsensus."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..stores._helpers import now_ms
from ..stores.graph_consensus import GRAPH_CONSENSUS_MIN, ConsensusResult, GraphStateEntry

logger = logging.getLogger(__name__)


class PgGraphConsensus:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def report_state(
        self,
        node_id: str,
        *,
        app: str,
        state_id: str,
        name: str,
        description: str,
        indicators: List[str],
        discovery_path: List[str],
        is_optional: bool,
        verification: str,
        transitions: Optional[Dict[str, str]] = None,
    ) -> ConsensusResult:
        if verification != "verified":
            return {"consensus": False, "confirmedCount": 0}

        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM nurture_graph_states WHERE app=$1 AND state_id=$2",
                app, state_id,
            )
            if existing is None:
                await conn.execute(
                    """
                    INSERT INTO nurture_graph_states
                      (state_id, app, name, description, indicators, discovery_path,
                       is_optional, source, discovered_by, discovered_at,
                       confirmed_by_nodes, consensus, transitions)
                    VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,'discovered',$8,$9,
                            $10::jsonb,FALSE,$11::jsonb)
                    ON CONFLICT (app, state_id) DO NOTHING
                    """,
                    state_id, app, name, description,
                    json.dumps(list(indicators)),
                    json.dumps(list(discovery_path)),
                    is_optional, node_id, now_ms(),
                    json.dumps([node_id]),
                    json.dumps(dict(transitions or {})),
                )
                return {"consensus": False, "confirmedCount": 1}

            confirmed = existing["confirmed_by_nodes"]
            if isinstance(confirmed, str):
                confirmed = json.loads(confirmed)
            confirmed = list(confirmed or [])
            if node_id in confirmed:
                return {
                    "consensus": bool(existing["consensus"]),
                    "confirmedCount": len(confirmed),
                }
            confirmed.append(node_id)
            consensus = len(confirmed) >= GRAPH_CONSENSUS_MIN

            current_indicators = existing["indicators"]
            if isinstance(current_indicators, str):
                current_indicators = json.loads(current_indicators)
            new_indicators = list(current_indicators or [])
            if consensus and len(indicators) > len(new_indicators):
                new_indicators = list(indicators)

            current_transitions = existing["transitions"]
            if isinstance(current_transitions, str):
                current_transitions = json.loads(current_transitions)
            new_transitions = {**(current_transitions or {}), **(transitions or {})}

            await conn.execute(
                """
                UPDATE nurture_graph_states
                SET confirmed_by_nodes = $1::jsonb,
                    consensus = $2,
                    indicators = $3::jsonb,
                    transitions = $4::jsonb
                WHERE app = $5 AND state_id = $6
                """,
                json.dumps(confirmed), consensus,
                json.dumps(new_indicators), json.dumps(new_transitions),
                app, state_id,
            )
        return {"consensus": consensus, "confirmedCount": len(confirmed)}

    async def get_states(self, app: str) -> List[GraphStateEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM nurture_graph_states WHERE app = $1", app
            )
        return [_row_to_entry(r) for r in rows]

    async def get_consensus_states(self, app: str) -> List[GraphStateEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM nurture_graph_states WHERE app = $1 AND consensus = TRUE",
                app,
            )
        return [_row_to_entry(r) for r in rows]

    async def get_apps(self) -> List[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT app FROM nurture_graph_states"
            )
        return [r["app"] for r in rows]


def _row_to_entry(row: Any) -> GraphStateEntry:
    def _decode(v: Any) -> Any:
        return json.loads(v) if isinstance(v, str) else v
    return GraphStateEntry(
        stateId=row["state_id"], app=row["app"], name=row["name"],
        description=row["description"] or "",
        indicators=list(_decode(row["indicators"]) or []),
        discoveryPath=list(_decode(row["discovery_path"]) or []),
        isOptional=bool(row["is_optional"]),
        source=row["source"] or "discovered",
        discoveredBy=row["discovered_by"] or "",
        discoveredAt=int(row["discovered_at"] or 0),
        confirmedByNodes=list(_decode(row["confirmed_by_nodes"]) or []),
        consensus=bool(row["consensus"]),
        transitions=dict(_decode(row["transitions"]) or {}),
    )
