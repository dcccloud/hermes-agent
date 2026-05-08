# 事件与轨迹上报管线 — Hermes Plugin Delta

**前置阅读:** OpenClaw [event-trace-pipeline.md](https://github.com/openclaw/openclaw/blob/main/docs/avatar-community/event-trace-pipeline.md)。本文档只列与 OpenClaw 不同的部分。

---

## 1. 不变内容

- **两类数据:Event(轻量生命周期)/ Trace(VLM 完整动作序列)**
- **生成机制:** `EventLog` + `TraceStore`(Python 设备服务内,从 OpenClaw 完整继承)
- **数据 schema:** `event_id` / `trace_id` / `agent_id` / `device_id` / `app` / `operation` / `step` / `actions` / ...
- **去重:** `ON CONFLICT DO NOTHING`(PG)/ 内存 find()(JSON)
- **容量:** 循环缓冲 events 20k / traces 10k(JSON 后端)
- **特殊查询:** `extractObservedData` / `getSuccessRate`
- **事件类型清单**(operation.start / step.complete / state.discovered / recipe.* 等)

---

## 2. 关键 delta:运输层

### 2.1 OpenClaw 模式

```
Python 服务
   ↓ HTTP /api/events (cursor 分页)
TS connector
   ↓ WebSocket: client.request("node.event", {event: "nurture.event.report", payloadJSON})
OpenClaw Gateway 路由
   ↓
社区扩展接收
```

### 2.2 Avatar-Hermes 模式

```
Python 服务(:8600,不变)
   ↓ HTTP /api/events (cursor 分页,不变)
Hermes plugin background thread
   ↓ HTTP POST /api/upload?kind=event (Bearer token)
社区 FastAPI(:18790)
   ↓
EventStore / TraceStore (Python)
```

详见 [http-protocol.md](http-protocol.md) 的 `POST /api/upload` 端点。

---

## 3. 上报构造代码

```python
# plugins/nurture/community_sync.py
async def build_event_payload(bridge, agent_id, cursor) -> tuple[list[dict], str]:
    batch = await bridge.get_events(
        cursor=cursor,
        limit=100,
        event_types="operation.complete,step.complete,state.discovered,recipe.*",
    )
    return batch["events"], batch["cursor"]

async def build_trace_payload(bridge, agent_id) -> list[dict]:
    raw = await bridge.get_pending_traces(limit=50)
    return [
        {
            "traceId": r["id"],
            "agentId": agent_id,
            "deviceId": r["device_id"],
            "app": infer_app(r["operation"]),
            "operation": r["operation"],
            "step": r["step"],
            "timestamp": r["timestamp"],
            "actions": [_normalize_action(a) for a in r["actions"]],
            "success": bool(r["success"]),
            "duration_ms": r.get("duration_ms"),
            "taskId": r.get("task_id"),
        }
        for r in raw
    ]
```

---

## 4. background thread 调度

```python
# plugins/nurture/__init__.py
def cron_loop():
    last = {"caps": 0, "trace": 0, "event": 0, "task": 0, "advice": 0, "directive": 0}
    while not stop_event.is_set():
        now = time.time()

        if now - last["caps"] >= 60:
            asyncio.run(report_capabilities(...))
            last["caps"] = now

        if now - last["trace"] >= 60:
            asyncio.run(report_traces(...))
            last["trace"] = now

        if now - last["event"] >= 60:
            asyncio.run(report_events(...))
            last["event"] = now

        if now - last["task"] >= 30:
            asyncio.run(poll_tasks(...))
            last["task"] = now
        # ... advice / directive 同上

        stop_event.wait(timeout=5)
```

5s tick + 各任务独立 schedule,避免漂移。

---

## 5. Cursor / Session 去重

延续 OpenClaw 设计:

- Event 上报用 `cursor` 分页,设备本地维护 `eventCursor`(session-scoped,重启从头)
- Trace 上报用 `reportedTraceIds: set[str]` 内存去重
- 社区端最终去重靠 `event_id` / `trace_id` 主键 + `ON CONFLICT DO NOTHING`

---

## 6. recipe.rolled_back 事件特殊处理

延续 OpenClaw 设计 — 这条事件 payload 包含被 rollback 的版本信息:

```python
{
    "operation": "facebook/like_video_1",
    "step": "main",
    "from_version": "698c46402955",
    "from_source": "community",
    "from_community_version": "node-other-aaaa11112222",
    "to_version": "13628f62a9fc",
    "to_source": "self",
}
```

社区端可以据此统计"哪个社区版本被多少节点拒掉",进一步剔除坏 recipe(Phase 5 analytics 用)。

---

## 7. 实现状态(目标)

Phase 1-3 完成时:

- ✓ Event 全链路:Python EventLog → /api/events → background thread → 社区 EventStore
- ✓ Trace 全链路:Python TraceStore → /api/traces/pending → background thread → 社区 TraceStore
- ✓ Cursor 分页(events)
- ✓ Pending 批量(traces)
- ✓ 去重(reportedTraceIds + 主键)
- ✓ JSON + PG 双后端
- ✓ 基础查询 API

未实现(同 OpenClaw):
- ✗ 事件流式推送(目前轮询拉取)
- ✗ Trace 压缩
- ✗ 跨 Agent 事件关联分析
- ✗ 实时告警
- ✗ 事件归档与冷热分层
