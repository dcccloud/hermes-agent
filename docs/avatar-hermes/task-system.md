# 任务系统 — Hermes Plugin Delta

**前置阅读:** OpenClaw [task-system.md](https://github.com/openclaw/openclaw/blob/main/docs/avatar-community/task-system.md)。本文档只列与 OpenClaw 不同的部分。

---

## 1. 关键 delta:Worker 执行机制

[decisions.md ADR-007](decisions.md#adr-007) 确定使用 Hermes Kanban 而非 OpenClaw subagent。

### 1.1 OpenClaw 模式(对照)

```
社区 push task → 设备 connector handleTaskDispatch
   ↓
subagent.run({sessionKey: "community-task:<id>"})
   ↓
等 subagent 跑完(60min 超时)
   ↓
读 community-tasks.json 看 outcome
   ↓
转发 nurture.task.complete

启动时: sweepStaleTaskSessions() 清理孤儿
```

### 1.2 Avatar-Hermes 模式

```
设备 background thread 拉到新 task (30s polling)
   ↓
plugins/nurture/kanban_bridge.py:
   kanban_create({
     title: "[Community Task <id>] <app>: <description>",
     description: <task.description>,
     metadata: {nurture_task_id, app, expires_at},
     assignee_profile: "nurture-task-worker",
     priority: <task.priority>,
   })
   ↓
Kanban dispatcher 自动 spawn worker hermes 实例(异步,不阻塞)
   ↓
worker hermes:
   - 加载 nurture plugin
   - pre_llm_call hook 注入 per-app persona
   - Pi Agent 收到 prompt 执行
   - kanban_complete(outcome=...)
   ↓
post_kanban_complete hook(在 nurture plugin 注册)转发:
   community_sync.report_task_outcome(taskId, outcome, ...)
   POST /api/task/complete
```

---

## 2. nurture-task-worker profile

需要预设一个 hermes profile 作为 worker 模板:

```bash
# Phase 0 安装时自动执行:
hermes profile create nurture-task-worker
hermes --profile nurture-task-worker config set tools.cli.enabled '["nurture", "kanban", "memory"]'
hermes --profile nurture-task-worker config set delegation.subagent_auto_approve true
hermes --profile nurture-task-worker config set plugins.nurture.enabled true
```

worker profile 的关键不同:
- 启用 nurture toolset(操作设备)
- 启用 kanban toolset(报告状态)
- 自动批准 — 不需要人工同意每个工具调用
- 不启用 community_sync 上行 thread(避免重复上报)

---

## 3. Kanban 替代 OpenClaw 的功能映射

| OpenClaw 功能 | Hermes Kanban 等价 |
|---|---|
| `subagent.run({sessionKey})` | `kanban_create + dispatcher spawn` |
| 60min 超时 | Kanban claim TTL + heartbeat |
| `sweepStaleTaskSessions` 启动清理 | Kanban dispatcher 自动 reclaim |
| failed/partial 重试 | Kanban 默认重试,5 次失败自动 block |
| refused 终态 | `kanban_block(reason="persona_conflict")` |
| `nurture_task_report` 工具 | `kanban_complete(outcome=...)` |
| 兜底:agent 没上报 视为 failed | Kanban worker 死亡时 dispatcher 自动 reclaim |

**结果:OpenClaw 手写的 ~150 LOC session lifecycle 代码在 Hermes 版本完全省掉。**

---

## 4. 数据模型(不变)

`CommunityTask` schema 与 OpenClaw 1:1。`community-tasks.json` 文件保持兼容。Kanban metadata 里嵌入 nurture task id 做关联。

```python
# plugins/nurture/kanban_bridge.py
def task_to_kanban(task: CommunityTask) -> KanbanCardSpec:
    return {
        "title": f"[Community Task {task.task_id}] {task.app}: {task.description[:60]}",
        "description": task.description,
        "metadata": {
            "nurture_task_id": task.task_id,
            "app": task.app,
            "expires_at": task.expires_at,
        },
        "priority": task.priority,
        "assignee_profile": "nurture-task-worker",
    }
```

worker 收到 prompt 时,prompt 头部包含 task_id,工具调用 `kanban_complete` 时 dispatcher 通过 metadata 关联回 nurture task。

---

## 5. Persona 缺失时 lazy 注入(保留 OpenClaw 行为)

worker hermes 启动时,nurture plugin 的 `pre_llm_call` hook 检查:

```python
def hook(task_id, session_id, **kwargs):
    # 如果是 community-task session(从 metadata 识别)
    task = get_community_task_from_session(session_id)
    if task and not bridge.persona_exists(task.app):
        return {
            "extra_system_prompt": (
                f"\n注意:{task.app} 的人设档案尚未建立。请先依次调用 "
                f"nurture_execute({task.app}.view_my_profile / "
                f"scrape_overview / scrape_fan_analysis / "
                f"scrape_content_analysis) 采集数据,再据此判断..."
            )
        }
    # 正常注入
    return inject_persona_goals_caps(...)
```

---

## 6. Outcome 与社区下架规则(不变)

- `outcome="success"` → 社区下架公告板
- `failed/partial/refused` → 公告板保 active,其他 agent 仍可接到
- TTL 过期 → 社区端 cleanup 任务清理

---

## 7. 实现状态(目标)

Phase 4 完成时:

- ✓ Kanban worker profile 预设
- ✓ Webhook → kanban_create bridge(实际上是 polling → kanban_create)
- ✓ post_kanban_complete hook 转发
- ✓ Persona lazy 注入
- ✓ 端到端联调:社区发任务 → kanban 执行 → 结果回报

未实现(同 OpenClaw):
- ✗ 任务优先级调度(目前按时间顺序)
- ✗ 任务依赖链
- ✗ 任务进度中间态上报
- ✗ 任务模板库
- ✗ Refused 任务的撤销重置
