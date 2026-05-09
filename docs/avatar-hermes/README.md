# Avatar-Hermes 系统文档

OpenClaw nurture/nurture-community 体系在 Hermes-Agent 框架上的对等实现。

## 实施状态

| Phase | 内容 | 状态 |
|---|---|---|
| 0 | 双插件脚手架 + 13 篇 design doc | ✓ |
| 1 | 设备侧 Python lift + tools + pre_llm_call hook | ✓ |
| 2 | 7 大社区 store JSON + PG 双后端翻译 | ✓ |
| 3 | MCP + HTTP polling 协议层 | ✓ |
| 4 | Kanban worker 任务系统 | ✓ |
| 5 | Layer 2 MVP — analytics + 周报 + Langfuse | ✓ |
| 6 | OpenClaw → Hermes 迁移工具 | ✓ |

测试覆盖:33 passed, 1 skipped(admin endpoints stub)。设备侧 + 社区侧 + 协议层 + Kanban 桥 + Analytics + 迁移全链路测试通过。

---

## 文档索引

### 核心契约(必读,实施前先读)

| 文档 | 内容 |
|---|---|
| [decisions.md](decisions.md) | 7 个关键决策的记录(ADR) |
| [boundary-contracts.md](boundary-contracts.md) | 系统不变量 — 运行时零 LLM、Recipe 数据流、persona 注入边界 |
| [architecture.md](architecture.md) | 整体架构、Hermes 与 OpenClaw 的差异、双语言边界 |
| [mcp-http-protocol.md](mcp-http-protocol.md) | 设备↔社区通信协议:MCP(同步查询)+ HTTP(上下行批量) |

### 模块设计(对照 OpenClaw 原文 + delta)

| 文档 | 内容 |
|---|---|
| [device-agent.md](device-agent.md) | 设备侧 Agent — Hermes plugin 形态下的实现 delta |
| [community-agent.md](community-agent.md) | 社区侧 Agent — Python 翻译后的实现 delta |
| [device-memory-evolution.md](device-memory-evolution.md) | 设备记忆系统 — 路径迁移到 ~/.hermes/nurture/data/ |
| [community-memory-evolution.md](community-memory-evolution.md) | 社区记忆系统 — 7 大 store 翻译为 Python |
| [task-system.md](task-system.md) | 任务系统 — Kanban worker 替代 OpenClaw subagent |
| [event-trace-pipeline.md](event-trace-pipeline.md) | 事件与轨迹上报管线 — HTTP 上报替代 WS |

### 运维与部署

| 文档 | 内容 |
|---|---|
| [configuration.md](configuration.md) | 配置项、profile 设置、目录结构、启动流程 |
| [migration.md](migration.md) | hermes nurture migrate — OpenClaw → Hermes 迁移工具 |
| [plugin-api-audit.md](plugin-api-audit.md) | Hermes 插件 API 表面审计(Phase 0 前置) |

---

## 一句话概述

**每台设备运行一个 Hermes 实例(装 nurture 插件即获得设备自动化能力);多个 Hermes 通过社区 Hermes 实例(装 nurture-community 插件)以纯 HTTP 协议共享 Recipe 与设备图谱,产生超越个体的集体智慧。**

---

## 与 OpenClaw 的关系

| 维度 | OpenClaw 原版 | Avatar-Hermes |
|---|---|---|
| 框架 | OpenClaw (TypeScript) | Hermes-Agent (Python) |
| 扩展机制 | `extensions/<name>/` + `openclaw.plugin.json` | `plugins/<name>/` + `plugin.yaml` |
| 设备↔社区通信 | WebSocket node 协议 | MCP(同步查询)+ HTTP(批量上下行) |
| 任务执行 | OpenClaw Subagent | Hermes Kanban worker |
| 子进程 | OpenClaw service registry | on_session_start + atexit |
| 周期任务 | setInterval (4 个 timer) | background thread |
| 多平台投递 | (无) | Hermes 原生(Telegram/Discord/Slack/...) |
| 持久后端 | PG + JSON 双后端 | PG + JSON 双后端(对齐) |

详细决策在 [decisions.md](decisions.md)。
