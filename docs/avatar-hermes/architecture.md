# 总体架构

本文档描述 Avatar-Hermes 系统的整体架构。**阅读前请先看 [decisions.md](decisions.md) 和 [boundary-contracts.md](boundary-contracts.md)**。本文档假定读者熟悉 OpenClaw [avatar-community/architecture.md](https://github.com/openclaw/openclaw/blob/main/docs/avatar-community/architecture.md) 的设计原则。

---

## 1. 愿景

OpenClaw avatar-community 的愿景在 Hermes 上完整保留:

**分布式 APP 自动化智能体系统 — 每台设备一个 Hermes 实例(装 nurture 插件),既是自主助手也是设备自动化引擎;多个 Hermes 通过社区 Hermes 实例(装 nurture-community 插件)共享经验、汇聚数据。**

**两层能力分工不变:**

| 层 | 核心问题 | 问题本质 | Agent 角色 | 社区角色 |
|---|---|---|---|---|
| **第一层:探索与学习** | 怎么操作 APP | 确定性 | 完全自主 | 加速器 |
| **第二层:策略与优化** | 怎么用好 APP | 统计性 | 数据采集 + 执行 | 核心智能 |

---

## 2. 系统全景

```
┌────────────────────────────────────────────────────────────────────┐
│  社区 Hermes 实例 (HERMES_HOME=~/.hermes/profiles/community/)        │
│  + plugins/nurture-community 插件                                    │
│                                                                      │
│  ┌────────────────────┐  ┌────────────────────┐                     │
│  │ KnowledgeEngine     │  │ Strategy Engine    │                     │
│  │ (第一层)             │  │ (第二层 MVP)         │                     │
│  │                     │  │                    │                     │
│  │ - RecipeStore       │  │ - analytics/       │                     │
│  │ - GraphConsensus    │  │   aggregator       │                     │
│  │ - RecipeFusion      │  │ - 周报投递          │                     │
│  │ - Distribution      │  │ - Langfuse trace   │                     │
│  └────────────────────┘  └────────────────────┘                     │
│                                                                      │
│  ┌────────────────────┐  ┌────────────────────┐                     │
│  │ AvatarRegistry      │  │ FastAPI :18790     │                     │
│  │ - 注册/发现          │  │ - /api/upload      │                     │
│  │ - 能力索引           │  │ - /api/{tasks,...}/poll │                │
│  │ - Bearer token bind  │  │ - /api/recipes/... │                     │
│  └────────────────────┘  └────────────────────┘                     │
│                                                                      │
│  PostgreSQL 17.5 (生产) / JSON (开发)                                │
└────────────────────────┬───────────────────────────────────────────┘
                         │ 纯 HTTP/JSON
                         │ Bearer token (avatarId-bound)
        ┌────────────────┼───────────────┐
        ▼                ▼               ▼
┌────────────┐    ┌────────────┐    ┌────────────┐
│ Hermes #1  │    │ Hermes #2  │    │ Hermes #3  │  设备侧 Hermes 实例
│ (抖音)      │    │ (抖音)      │    │ (快手)      │  + plugins/nurture
│            │    │            │    │            │  (各 profile 独立)
└─────┬──────┘    └─────┬──────┘    └─────┬──────┘
      │                 │                 │
      ▼                 ▼                 ▼
┌──────────────────────────────────────────────────┐
│  Python Device Service (Hermes plugin 子进程)      │
│  FastAPI :8600 (per-instance, profile-isolated)   │
│                                                   │
│  ┌────────────┐  ┌────────────────────────┐       │
│  │ Operations │  │ Learning Engine        │       │
│  │ douyin.*   │  │ - AdaptiveStep         │       │
│  │ xingtu.*   │  │ - RecipeStore          │       │
│  │ haosheng.* │  │ - RecipeGenerator      │       │
│  └────────────┘  │ - RecipeReviewer       │       │
│                  │ - AppGraph             │       │
│  ┌────────────┐  │ - StateDiscovery       │       │
│  │ Collector  │  │ - TraceStore           │       │
│  └────────────┘  │ - EventLog             │       │
│                  └────────────────────────┘       │
│                                                   │
│  ┌──────────────────────────────────────┐         │
│  │ PhoneAgent (ADB/u2)                  │         │
│  │ - device.click, swipe, dump_xml      │         │
│  │ - VLM 兜底 (vision + action)         │         │
│  └──────────────────────────────────────┘         │
└──────────────────────────────────────────────────┘
```

---

## 3. 通信通道

设备 hermes 实例内部 / 跨进程 / 跨主机的通信结构:

```
┌──────────────────────────────────────────────────────────┐
│  设备 hermes 主进程                                        │
│                                                          │
│  Pi Agent (LLM 区,run_agent.py)                          │
│   │                                                      │
│   ├─ tool: nurture_execute        ─┐                     │
│   ├─ tool: nurture_task_report    ─┼─ 本地 hermes 工具表  │
│   └─ tool: nurture.* (MCP-discovered) ─┐                 │
│                                    │  │                  │
│  plugins/nurture/__init__.py       │  │                  │
│  ├─ ctx.register_tool             ─┘  │                  │
│  ├─ ctx.register_hook (pre_llm_call,  │                  │
│  │                     on_session_start)                 │
│  ├─ register_mcp_servers(...)        ─┘                  │
│  ├─ ctx.register_cli_command                             │
│  └─ background thread (6 个周期任务)                      │
└────┬─────────────┬───────────────────┬───────────────────┘
     │             │                   │
     │ ① 本地 HTTP │ ② MCP HTTP        │ ③ HTTP REST
     │ httpx       │ Streamable        │ httpx
     │             │ (mcp_tool 自动     │ (background thread
     │             │  管理)            │  + token)
     │             │                   │
     ▼             │                   │
┌──────────────────┼───────┐           │
│  Python Device   │       │           │
│  Service (子进程) │       │           │
│  FastAPI :8600   │       │           │
│                  │       │           │
│  /api/execute    │       │           │
│  /api/capabilities       │           │
│  /api/recipes/*  │       │           │
│  /api/graph/*    │       │           │
│  /api/traces/*   │       │           │
│  /api/events     │       │           │
│  /api/persona/*  │       │           │
│  /api/goals      │       │           │
└──────────────────┘       │           │
                           │           │
                           ▼           ▼
                ┌──────────────────────────────────┐
                │  社区 hermes 实例(可远程)         │
                │  FastAPI :18790                  │
                │                                  │
                │  ② /mcp (MCP server endpoint)    │
                │     - nurture.recipes.get        │
                │     - nurture.graph.get          │
                │     - nurture.event.query        │
                │                                  │
                │  ③ REST endpoints                │
                │     - POST /api/upload           │
                │     - GET  /api/tasks/poll       │
                │     - GET  /api/advices/poll     │
                │     - GET  /api/directives/poll  │
                │     - POST /api/task/complete    │
                │     - POST /api/avatar/register  │
                └──────────────────────────────────┘
```

**三条通道分工:**

| 通道 | 协议 | 用途 | 谁消费 |
|---|---|---|---|
| ① 本地 HTTP | httpx → :8600 | Pi Agent 调本地设备操作 | nurture_execute / nurture_task_report 工具 handler |
| ② MCP HTTP | StreamableHTTP → :18790/mcp | Pi Agent 同步查询社区知识 | mcp_tool 自动 discover 后注册到工具表 |
| ③ REST HTTP | httpx → :18790/api/* | 周期上下行批量数据 | plugins/nurture 的 background thread |

**与 OpenClaw 的关键差异:**
- OpenClaw 用单一 WebSocket node 协议覆盖所有通信(本地 invoke / 社区 invoke / event 推送)
- Avatar-Hermes 三条通道职责清晰,各用最适合的协议:HTTP for 本地 / MCP for 同步 / REST for 批量

详见 [mcp-http-protocol.md](mcp-http-protocol.md)。

---

## 4. 文件结构

```
plugins/nurture/                              # 设备侧
├── plugin.yaml
├── __init__.py                               # register(ctx)
├── config.py                                 # NurtureConfig (pydantic)
├── service.py                                # Python 子进程 lifecycle
├── device_bridge.py                          # httpx → :8600
├── device_tools.py                           # nurture_execute / nurture_task_report
├── community_mcp.py                          # register_mcp_servers 调用,管 token header
├── community_sync.py                         # capability/trace/event 上报 builder
├── community_poll.py                         # task/advice/directive 轮询 handler
├── prompt_hook.py                            # pre_llm_call 注入 persona/goals/caps
├── kanban_bridge.py                          # task → kanban_create + complete 转发
├── migrate.py                                # OpenClaw → Hermes 迁移
├── cli.py                                    # hermes nurture <verb>
├── tests/                                    # pytest
└── python/                                   # lift openclaw_agent/(无重构)
    ├── server.py
    ├── requirements.txt
    └── openclaw_agent/...                    # 132 个 .py 文件

plugins/nurture-community/                    # 社区侧
├── plugin.yaml
├── __init__.py                               # register(ctx) — 起 FastAPI
├── community_server.py                       # FastAPI :18790 (REST + /mcp endpoint)
├── mcp_server.py                             # MCP server 工具暴露(基于 mcp_serve.py 模板)
├── knowledge_engine.py                       # 双后端门面(JSON / PG)
├── stores/                                   # 7 大 store
│   ├── recipe_store.py / pg_recipe_store.py
│   ├── graph_consensus.py / pg_graph_consensus.py
│   ├── event_store.py / pg_event_store.py
│   ├── trace_store.py / pg_trace_store.py
│   ├── task_dispatch.py / pg_task_dispatch.py
│   ├── advice_engine.py / pg_advice_engine.py
│   ├── directive_engine.py / pg_directive_engine.py
│   └── value_schema.py
├── pg/
│   ├── pool.py                               # asyncpg pool
│   └── migration.sql                         # 直接复用 OpenClaw 版本
├── recipe_fusion.py                          # 用 hermes auxiliary_client
├── distribution.py
├── avatar_registry.py
├── analytics/                                # Phase 5 占位
│   └── aggregator.py
├── cli.py                                    # hermes nurture-community <verb>
└── tests/
```

---

## 5. 模块交互流程

### 5.1 单设备 operation 执行(运行时,跟 OpenClaw 一致)

```
Pi Agent 收到任务
   │
   │ 1 次 LLM call: 决定调 nurture_execute
   ▼
ctx.register_tool 注册的 nurture_execute handler
   │ httpx POST 到 :8600 /api/execute
   ▼
Python Device Service 加载 Operation(douyin.enter_data_center)
   │
   ▼
Operation 包含多个 AdaptiveStep:
   Step 1: go_to_me_tab
   Step 2: go_to_creator_center
   Step 3: go_to_data_center
   │
   ▼
每个 Step:
   Tier 0: detect_page() == target? → 跳过
   Tier 1: Recipe 存在? → exec → verify (0 LLM)
   Tier 2: VLM 兜底 → 保存 trace → state discovery (烧 vision token)
   │
   ▼
执行结果 + 指标 → 本地存储
   │
   ▼
HTTP 响应给 Pi Agent → Pi Agent 进入下一步
```

### 5.2 设备↔社区同步(60s background thread)

```
设备 background thread (每 60s):
   │
   ├─ buildCapabilityPayload → POST /api/upload?kind=capability
   │   └─ 响应: bestRecipes 数组 → 写 community-recipes-cache.json
   ├─ buildTracePayload      → POST /api/upload?kind=trace
   └─ buildEventPayload      → POST /api/upload?kind=event

设备 background thread (每 30s):
   │
   ├─ GET /api/tasks/poll      → 写 community-tasks.json
   ├─ GET /api/advices/poll    → 写 advices.json
   └─ GET /api/directives/poll → 写 directives.json

社区:
   │
   ├─ /api/upload 处理
   │   ├─ recipeStore.ingestFromCapability()
   │   ├─ graphConsensus.reportState()
   │   ├─ traceStore.ingest()
   │   └─ eventStore.ingest()
   │
   └─ 后台 cron(社区端):
       ├─ recipe-fusion (LLM)
       ├─ analytics 聚合 (Phase 5)
       └─ 周报投递 (Phase 5)
```

### 5.3 社区任务执行(Phase 4 — Kanban 模式)

```
设备 background thread 拉到新 task
   │
   ▼
plugins/nurture/kanban_bridge.py:
   kanban_create({
     title: "[Community Task <id>] <app>",
     description: <task.description>,
     metadata: {nurture_task_id, app},
     assignee_profile: "nurture-task-worker",
   })
   │
   ▼
Kanban dispatcher 自动 spawn worker hermes 实例
   │
   ▼
worker hermes 启动:
   ├─ 加载 nurture plugin
   ├─ pre_llm_call hook 注入 per-app persona
   ├─ Pi Agent 收到 prompt: "[Community Task ...]"
   ├─ 执行 nurture_execute / 设备工具
   └─ kanban_complete(outcome=success/failed/refused/partial)
   │
   ▼
post_kanban_complete hook (在 nurture plugin 注册):
   → community_sync.report_task_outcome()
   → POST /api/task/complete
   │
   ▼
社区:outcome=success 才下架公告板,其他保 active
```

---

## 6. 技术栈

| 层 | 技术 |
|---|---|
| 框架 | Hermes-Agent (Python 3.11+) |
| 设备插件 (Python) | hermes plugin + asyncio + httpx |
| 设备服务 (Python) | FastAPI + uvicorn + uiautomator2 |
| 社区插件 (Python) | hermes plugin + FastAPI + asyncpg |
| 社区数据库 | PostgreSQL 17.5 (生产) / JSON (开发) |
| LLM (Pi Agent) | hermes 配置的任意 provider |
| LLM (Recipe Fusion) | hermes auxiliary_client(可独立配 cheap model) |
| VLM | OpenAI 兼容 Vision API(Python 侧,与 OpenClaw 一致) |
| 设备通信 | ADB / u2 / HDC / XCTest |

---

## 7. 设计原则

继承 OpenClaw avatar-community/architecture.md 的 11 条原则,**不变**。Hermes 特有补充:

12. **MCP + HTTP 双通道协议**:同步查询走 MCP(Pi Agent 自动发现工具),批量上下行走 HTTP REST。不引入 WebSocket / 状态化协议
13. **Profile 隔离**:每个 hermes 实例 = 一个独立 profile,数据互不污染
14. **Hermes 平台投递**:Layer 2 报告/告警直接复用 Hermes 多平台投递能力(Telegram/Discord/Slack/...)
15. **变通先行,上游后议**:遇到 hermes plugin API 缺口先用插件内变通方案,稳定后再考虑提 PR

---

## 8. 实现状态对照

| OpenClaw avatar-community Phase | 状态 | Avatar-Hermes 对应 Phase |
|---|---|---|
| Phase 0: 两层能力分工 + 知识协议 | 设计文档不变 | Pre-Phase 0 |
| Phase 1: Agent 代码重组 | 设计不变 | Phase 1 (lift) |
| Phase 2: 社区模块化 | 设计不变 | Phase 2 (翻译) |
| Phase 3: 扩展化重构 | 改 hermes plugin | Phase 0 (脚手架) |
| Phase 4: 策略智能引擎 | OpenClaw 未实现 | Phase 5 MVP |
| Phase 5: 跨 APP / 信任评级 / P2P | OpenClaw 未实现 | 不在本次范围 |
