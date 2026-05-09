"""GraphConsensus (JSON backend) — multi-node graph state consensus.

Tracks which nodes have independently discovered/confirmed each APP page
state. A state reaches consensus when confirmed by >= GRAPH_CONSENSUS_MIN
distinct nodes, making it safe to distribute.

PG backend: see ``plugins/nurture-community/pg/pg_graph_consensus.py``.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from ._helpers import load_json, now_ms, save_json

logger = logging.getLogger(__name__)

GRAPH_CONSENSUS_MIN = 2


@dataclass
class GraphStateEntry:
    stateId: str
    app: str
    name: str
    description: str
    indicators: List[str] = field(default_factory=list)
    discoveryPath: List[str] = field(default_factory=list)
    isOptional: bool = False
    source: str = "discovered"
    discoveredBy: str = ""
    discoveredAt: int = 0
    confirmedByNodes: List[str] = field(default_factory=list)
    consensus: bool = False
    transitions: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GraphStateEntry":
        return cls(
            stateId=data.get("stateId", ""),
            app=data.get("app", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            indicators=list(data.get("indicators") or []),
            discoveryPath=list(data.get("discoveryPath") or []),
            isOptional=bool(data.get("isOptional", False)),
            source=data.get("source", "discovered"),
            discoveredBy=data.get("discoveredBy", ""),
            discoveredAt=int(data.get("discoveredAt", 0) or 0),
            confirmedByNodes=list(data.get("confirmedByNodes") or []),
            consensus=bool(data.get("consensus", False)),
            transitions=dict(data.get("transitions") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ConsensusResult(TypedDict):
    consensus: bool
    confirmedCount: int


class GraphConsensus:
    def __init__(self, store_dir: Path):
        self._store_file = Path(store_dir) / "graph.json"
        raw = load_json(self._store_file, [])
        self._states: List[GraphStateEntry] = [
            GraphStateEntry.from_dict(s) for s in raw if isinstance(s, dict)
        ]

    def report_state(
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
        """Report that *node_id* has independently confirmed a graph state.

        Only accepts ``verification == "verified"`` reports. New states
        start with consensus=false; reach consensus once
        ``len(confirmedByNodes) >= GRAPH_CONSENSUS_MIN`` (default 2).
        """
        logger.info(
            "graph-consensus reportState: stateId=%s verification=%s nodeId=%s",
            state_id, verification, node_id,
        )
        if verification != "verified":
            logger.info("graph-consensus reportState: skipping non-verified state")
            return {"consensus": False, "confirmedCount": 0}

        existing = next(
            (s for s in self._states if s.app == app and s.stateId == state_id),
            None,
        )

        if existing is not None:
            if node_id not in existing.confirmedByNodes:
                existing.confirmedByNodes.append(node_id)
                existing.consensus = (
                    len(existing.confirmedByNodes) >= GRAPH_CONSENSUS_MIN
                )
                if existing.consensus and len(indicators) > len(existing.indicators):
                    existing.indicators = list(indicators)
                if transitions:
                    existing.transitions = {**existing.transitions, **transitions}
                self._save()
            return {
                "consensus": existing.consensus,
                "confirmedCount": len(existing.confirmedByNodes),
            }

        entry = GraphStateEntry(
            stateId=state_id,
            app=app,
            name=name,
            description=description,
            indicators=list(indicators),
            discoveryPath=list(discovery_path),
            isOptional=is_optional,
            source="discovered",
            discoveredBy=node_id,
            discoveredAt=now_ms(),
            confirmedByNodes=[node_id],
            consensus=False,
            transitions=dict(transitions or {}),
        )
        self._states.append(entry)
        self._save()
        return {"consensus": False, "confirmedCount": 1}

    def get_states(self, app: str) -> List[GraphStateEntry]:
        return [s for s in self._states if s.app == app]

    def get_consensus_states(self, app: str) -> List[GraphStateEntry]:
        return [s for s in self._states if s.app == app and s.consensus]

    def get_apps(self) -> List[str]:
        return list(dict.fromkeys(s.app for s in self._states))

    def _save(self) -> None:
        save_json(self._store_file, [s.to_dict() for s in self._states])
