"""Phase 5 analytics tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_plugins.nurture_community.analytics import (
    aggregate_all,
    aggregate_app_metrics,
    build_weekly_report,
)
from hermes_plugins.nurture_community.knowledge_engine import KnowledgeEngine


@pytest.mark.asyncio
async def test_aggregate_app_metrics_empty(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")
    metrics = await aggregate_app_metrics(eng, "douyin")
    assert metrics.total_traces == 0
    assert metrics.success_rate == 0.0


@pytest.mark.asyncio
async def test_aggregate_app_metrics_with_traces(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")

    # Seed traces — 3 success, 1 failure for op_a
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    for i in range(3):
        await eng.ingest_trace({
            "traceId": f"a{i}", "agentId": f"agent-{i % 2}", "deviceId": f"d{i}",
            "app": "douyin", "operation": "op_a", "step": "main",
            "timestamp": yesterday,
            "actions": [], "success": True, "duration_ms": 1000 + i * 100,
        })
    await eng.ingest_trace({
        "traceId": "a-fail", "agentId": "agent-0", "deviceId": "d0",
        "app": "douyin", "operation": "op_a", "step": "main",
        "timestamp": yesterday,
        "actions": [], "success": False, "duration_ms": 500,
    })

    metrics = await aggregate_app_metrics(eng, "douyin")
    assert metrics.total_traces == 4
    assert metrics.success_count == 3
    assert metrics.success_rate == pytest.approx(0.75, abs=0.01)
    assert metrics.distinct_agents == 2
    assert "op_a" in metrics.operations
    op = metrics.operations["op_a"]
    assert op.total_traces == 4
    assert op.success_rate == pytest.approx(0.75, abs=0.01)
    # avg duration over 4 traces: (1000+1100+1200+500)/4 = 950
    assert op.avg_duration_ms == pytest.approx(950, abs=1)


@pytest.mark.asyncio
async def test_aggregate_all_skips_apps_without_traces(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")
    # Timestamp 1h in the past so it falls inside the default 7-day window.
    ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace(
        "+00:00", "Z"
    )
    await eng.ingest_trace({
        "traceId": "x1", "agentId": "a", "deviceId": "d",
        "app": "douyin", "operation": "op", "step": "main",
        "timestamp": ts, "actions": [], "success": True,
    })
    report = await aggregate_all(eng)
    assert "douyin" in report.apps
    assert report.total_traces == 1


@pytest.mark.asyncio
async def test_weekly_report_silent_when_empty(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")
    text = await build_weekly_report(eng)
    assert text.startswith("[SILENT]")


@pytest.mark.asyncio
async def test_weekly_report_renders_markdown(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")

    # Seed a recipe so recipe_coverage > 0
    await eng.ingest_capability_update("node-1", "Pixel-7", {
        "apps": {
            "douyin": {
                "operations": [{
                    "id": "douyin.like", "name": "like",
                    "steps": [{
                        "step": "main", "has_recipe": True,
                        "success": 100, "failure": 5, "success_rate": 0.95,
                        "disabled": False,
                        "code": "async def execute(d): return True",
                    }],
                }],
            },
        },
    })

    # Seed traces — many for "like" (which has a recipe + high success)
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    for i in range(15):
        await eng.ingest_trace({
            "traceId": f"l{i}", "agentId": "a", "deviceId": "d",
            "app": "douyin", "operation": "like", "step": "main",
            "timestamp": ts, "actions": [], "success": True,
            "duration_ms": 800,
        })

    text = await build_weekly_report(eng)
    assert text.startswith("# Avatar-Hermes weekly report")
    assert "douyin" in text
    assert "Total traces: **15**" in text
    assert "## Wins" in text
    # The "like" op should appear in wins (15 traces, 100% success, has recipe)
    assert "douyin/like" in text


@pytest.mark.asyncio
async def test_weekly_report_surfaces_risks(tmp_path: Path) -> None:
    eng = KnowledgeEngine(store_dir=tmp_path / "knowledge")
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    # 12 traces, 2 success — 16.7% rate (below 50% threshold)
    for i in range(12):
        await eng.ingest_trace({
            "traceId": f"f{i}", "agentId": "a", "deviceId": "d",
            "app": "douyin", "operation": "swipe", "step": "main",
            "timestamp": ts, "actions": [], "success": i < 2,
        })
    text = await build_weekly_report(eng)
    assert "## Risks" in text
    assert "douyin/swipe" in text
    assert "16.7%" in text
