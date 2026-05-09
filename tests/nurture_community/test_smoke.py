"""End-to-end smoke test for the JSON-backed KnowledgeEngine.

Exercises all 8 stores through the public facade. Run with::

    pytest tests/nurture_community/ -o addopts=""

The PG backend is exercised separately (gated behind
``NURTURE_COMMUNITY_TEST_DATABASE_URL`` since it needs a live PostgreSQL).

Plugin module loading is handled by ``conftest.py`` in this directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_plugins.nurture_community.knowledge_engine import KnowledgeEngine
from hermes_plugins.nurture_community.stores.directive_engine import (
    DirectiveInstruction,
)
from hermes_plugins.nurture_community.stores.task_dispatch import (
    TaskRequirements,
)


@pytest.mark.asyncio
async def test_capability_ingest_and_recipe_flow(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")
    assert eng.is_pg is False

    # Ingest 2 nodes' capability for the same step
    for node_id, model, success, failure in [
        ("node-A", "Pixel-7", 8, 2),
        ("node-B", "Pixel-8", 5, 5),
    ]:
        await eng.ingest_capability_update(node_id, model, {
            "apps": {
                "douyin": {
                    "operations": [{
                        "id": "douyin.give_a_like",
                        "name": "give_a_like",
                        "steps": [{
                            "step": "main",
                            "has_recipe": True,
                            "success": success,
                            "failure": failure,
                            "success_rate": success / (success + failure),
                            "disabled": False,
                            "code": f"async def execute(d): return {success > failure}",
                        }],
                    }],
                    "graph_summary": {
                        "verified_discovered_pages": [{
                            "state_id": "data_center",
                            "name": "Data Center",
                            "description": "douyin data hub",
                            "indicators": ["overview"],
                            "discovery_path": [],
                            "is_optional": False,
                            "verification": "verified",
                        }],
                    },
                },
            },
        })

    versions = await eng.get_recipe_versions("douyin", "give_a_like", "main")
    assert len(versions) == 2

    best = await eng.get_best_recipe("douyin", "give_a_like", "main")
    assert best is not None
    assert best.globalSuccessRate == pytest.approx(0.8, abs=0.01)

    consensus = await eng.get_consensus_graph_states("douyin")
    assert len(consensus) == 1
    assert "node-A" in consensus[0].confirmedByNodes
    assert "node-B" in consensus[0].confirmedByNodes


@pytest.mark.asyncio
async def test_trace_and_event_ingest(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")
    await eng.ingest_trace({
        "traceId": "t1", "agentId": "a1", "deviceId": "d1",
        "app": "douyin", "operation": "give_a_like", "step": "main",
        "timestamp": "2026-05-08T10:00:00Z",
        "actions": [], "success": True, "duration_ms": 1234,
    })
    traces = await eng.get_traces_by_app("douyin")
    assert len(traces) == 1
    assert traces[0].traceId == "t1"

    await eng.ingest_event({
        "eventId": "e1", "agentId": "a1", "deviceId": "d1",
        "eventType": "operation.complete",
        "timestamp": "2026-05-08T10:00:00Z",
        "operation": "douyin.give_a_like", "step": "main",
        "payload": {"ok": True},
    })
    events = await eng.get_recent_events()
    assert len(events) == 1
    assert events[0].eventType == "operation.complete"


@pytest.mark.asyncio
async def test_task_lifecycle(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")

    task = await eng.create_task(
        requirements=TaskRequirements(app="douyin", platform=["android"]),
        app="douyin",
        description="Give 5 likes",
    )
    assert task.status == "active"

    # Matching avatar gets it
    avail = await eng.get_available_tasks(platform="android", capabilities=[])
    assert len(avail) == 1
    assert avail[0].taskId == task.taskId

    # Wrong platform — filtered out
    avail_ios = await eng.get_available_tasks(platform="ios", capabilities=[])
    assert len(avail_ios) == 0

    # Complete
    completed = await eng.complete_task(task.taskId)
    assert completed is not None
    assert completed.status == "completed"
    avail_after = await eng.get_available_tasks(platform="android", capabilities=[])
    assert len(avail_after) == 0


@pytest.mark.asyncio
async def test_directive_delivery_tracking(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")

    d = await eng.create_directive(
        app="douyin",
        type="weight",
        summary="Boost food x2",
        instructions=[DirectiveInstruction(
            action="adjust_weight", target="food", params={"factor": 2.0},
        )],
    )

    pending = await eng.get_pending_directives("avatar-x", "douyin")
    assert len(pending) == 1

    await eng.mark_directive_delivered(d.directiveId, "avatar-x")
    pending_after = await eng.get_pending_directives("avatar-x", "douyin")
    assert len(pending_after) == 0

    # Other avatars still see it
    pending_other = await eng.get_pending_directives("avatar-y", "douyin")
    assert len(pending_other) == 1


@pytest.mark.asyncio
async def test_advice_high_failure_rate(tmp_path: Path) -> None:
    """Built-in high-failure-rate rule fires when an op has >=5 traces and
    success rate < 50%."""
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")

    for i in range(6):
        await eng.ingest_trace({
            "traceId": f"t{i}", "agentId": "a1", "deviceId": "d1",
            "app": "douyin", "operation": "swipe", "step": "main",
            "timestamp": "2026-05-08T10:00:00Z",
            "actions": [], "success": False, "duration_ms": 100,
        })

    count = await eng.analyze_and_generate_advice("douyin")
    assert count == 1

    pending = await eng.get_pending_advice("agent-X", "douyin")
    assert len(pending) == 1
    assert pending[0].type == "risk"


@pytest.mark.asyncio
async def test_value_schema_versioning(tmp_path: Path) -> None:
    """ValueSchema is the only always-JSON store; verify version bumps."""
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")
    from hermes_plugins.nurture_community.stores.value_schema import (
        ValueField,
        ValueSchemaCategory,
    )

    snap = eng.get_value_schema_snapshot()
    assert snap.version == 0

    eng.update_value_schema(ValueSchemaCategory(
        app="douyin", category="overview",
        fields=[ValueField(
            name="follower_count", description="主页粉丝数",
            observe_hint="screen text 'fans'", data_type="number",
            priority="high",
        )],
    ))
    snap2 = eng.get_value_schema_snapshot()
    assert snap2.version == 1
    assert len(snap2.schemas) == 1


@pytest.mark.asyncio
async def test_recipe_persistence_round_trip(tmp_path: Path) -> None:
    """Stores survive process restart (JSON files reloaded on construction)."""
    store_dir = tmp_path / "knowledge"

    eng1 = KnowledgeEngine(store_dir=store_dir)
    await eng1.ingest_capability_update("node-A", "Pixel-7", {
        "apps": {
            "douyin": {
                "operations": [{
                    "id": "douyin.x", "name": "x",
                    "steps": [{
                        "step": "main", "has_recipe": True,
                        "success": 1, "failure": 0, "success_rate": 1.0,
                        "disabled": False, "code": "async def execute(d): pass",
                    }],
                }],
                "graph_summary": {},
            },
        },
    })

    # Fresh engine on same dir reads existing data
    eng2 = KnowledgeEngine(store_dir=store_dir)
    versions = await eng2.get_recipe_versions("douyin", "x", "main")
    assert len(versions) == 1
