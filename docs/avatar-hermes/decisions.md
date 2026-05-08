# 关键决策记录(ADR)

本文档记录 Avatar-Hermes 项目启动时锁定的 7 个关键决策。每条决策一旦记录在此,Phase 0-6 实施期间不得擅自更改。如需变更,先修改本文档并附变更原因。

---

## ADR-001:设备↔社区协议层 = MCP + HTTP polling

**状态:** Accepted (2026-05-07,初版选"纯 HTTP",2026-05-07 修订:复查 audit 后发现 Hermes 已有完整 MCP 体系且 plugin 可调 `register_mcp_servers()` 公开 API,改回 MCP + HTTP polling)

**上下文:**
OpenClaw 用自家 Gateway WebSocket node 协议。Hermes 不支持 plugin 注册 webhook 路由([plugin-api-audit](plugin-api-audit.md) 缺口),但**支持 plugin 调 `tools.mcp_tool.register_mcp_servers(dict)` 注册 MCP server**(stdio + HTTP/StreamableHTTP 双传输,自带 OAuth + 重连 + tool 过滤 + sampling)。

**决策:**
- **同步查询(recipes/graph/event-query):** plugin 调 `register_mcp_servers()` 注册社区 MCP server URL,带 Bearer token header。Pi Agent 自动发现 `nurture.recipes.get` / `nurture.graph.get` / `nurture.event.query` 等远端工具
- **上行批量(capability/trace/event):** background thread 周期(60s)POST 到社区 FastAPI `/api/upload`
- **下行批量(task/advice/directive):** background thread 轮询(30s)GET 社区 FastAPI `/api/{tasks,advices,directives}/poll`(替代 push,因 hermes 不支持 plugin 注册 webhook 路由)
- **鉴权:** Bearer token(token bind avatarId,既用于 MCP HTTP headers,也用于 FastAPI Authorization)
- **协议详情:** [mcp-http-protocol.md](mcp-http-protocol.md)

**理由:**
1. MCP 是 Hermes 一等公民,plugin 集成成本几乎为零(一行 register_mcp_servers 调用)
2. Pi Agent 自动发现工具,跟 OpenClaw 原版 nodes 工具体验对等
3. 上游 hermes MCP 改进(OAuth/sampling/tool 过滤)直接受益,无需 backport
4. 仍有 webhook 缺口,下行保留 polling(30s 间隔可接受)
5. Phase 3 工作量与"纯 HTTP"方案相比 +0.3d,但工具表更干净

**后果:**
- Plugin pip_dependencies 加 `mcp` 包(其实 hermes 自身依赖,影响极小)
- 社区侧需要起 MCP server endpoint(基于 `mcp_serve.py` 模板,~150 LOC)
- 下行实时性仍受 30s polling 限制
- 长期可向 hermes 上游提 `register_webhook_route` PR 切换下行到 push

---

## ADR-002:存储后端 = PG + JSON 双后端

**状态:** Accepted

**决策:**
社区侧 7 大 store 各实现 JSON 与 PostgreSQL 两个后端,通过 `databaseUrl` 配置切换。完全对齐 OpenClaw。

**理由:**
- JSON:本地开发零依赖
- PG:多设备生产环境(并发安全 + 持久化)
- migration.sql 直接复用 OpenClaw 版本(asyncpg 替代 node-pg)

**后果:**
- 翻译工作量翻倍(每个 store 两份实现)
- 但语义清晰,生产/开发一致

---

## ADR-003:Layer 2 策略智能 = MVP

**状态:** Accepted

**决策:**
Phase 5 仅打通管线:trace 离线聚合 cron + 1 个示例 advice 规则 + 1 个示例 directive + 周报多平台投递 + Langfuse 接入。不做完整 A/B 实验框架。

**理由:**
- Layer 2 是 OpenClaw 文档明确未实现的部分,本次不背负超出范围目标
- MVP 验证管线后,后续可独立项目扩展

**后果:**
- 不能开箱即用做 A/B 实验
- 但比 OpenClaw 多了多平台投递与 Langfuse,已有差异化

---

## ADR-004:迁移工具 = `hermes nurture migrate`(纯文件复制)

**状态:** Accepted

**决策:**
提供 `hermes nurture migrate --source ~/.openclaw` 命令。**因 ADR-005 选择保守方案,迁移退化为纯文件复制,无 schema 转换。**

**理由:**
- OpenClaw 与 Hermes 设备侧文件 schema 完全一致(meta.json / app_graphs / traces / persona / goals)
- 只需路径迁移 `~/.openclaw/nurture-workspaces/...` → `~/.hermes/nurture/...`
- 零转换风险

**后果:**
- 用户从 OpenClaw 迁移成本极低
- 实施工作量小(~1d)

---

## ADR-005:Recipe 与 Hermes Skills 关系 = 完全独立(保守方案)

**状态:** Accepted (在讨论中改过一次,最终选保守)

**决策:**
Recipe 完全不进 Hermes Skills 体系。
- Recipe 文件位于 `~/.hermes/nurture/data/recipes/<app>/<op>/<step>/`
- schema 与 OpenClaw 1:1
- `~/.hermes/skills/` 不放任何 nurture 资产
- Curator 不接触 nurture
- 可见性通过 `hermes nurture list` 子命令提供

**理由:**
- 三大要求(零 token / 探索用 token / 复用是脚本)由 AdaptiveStep 保证,与 Skills 集成无关
- Hermes 没有 `curator_skip_filter`,激进方案需要 monkey-patch
- 保守方案运行时只有一条路径,认知负担最小
- 未来要发布 Skills Hub 任何时候都可加 SkillAdapter,不被锁死

**后果:**
- 失去 Curator promote/archive 复用,但 RecipeReviewer 已有等价实现
- `hermes skills list` 看不到 recipes(用 `hermes nurture list` 替代)
- Phase 1 工作量 -0.5d

---

## ADR-006:Persona/Goals = 独立文件 + pre_llm_call 注入

**状态:** Accepted

**决策:**
- 文件位置:`~/.hermes/nurture/agent/{goals.md, persona/<app>.md}`
- 注入方式:nurture plugin 注册 `pre_llm_call` hook,在 system prompt 里加 `<nurture-persona>` / `<nurture-goals>` / `<nurture-capabilities>` XML 块
- **不与 SOUL.md 合并**(避免与用户自维护的 SOUL.md 冲突)
- **不进 Hermes context files**(语义不贴合)

**理由:**
- 主人可以直接编辑 markdown 文件
- 与 Hermes 自身的人设机制完全解耦
- pre_llm_call 是 Hermes 文档明确推荐的 system prompt 注入点
- 每个 app 独立 persona,符合 OpenClaw 的 lazy-init 模式

**注意事项(prompt caching):**
- Hermes 强约束:不能在 mid-conversation 改 system prompt
- nurture persona 注入只在 session 起始或 compress 后生效
- persona 文件被外部修改时,需要等下次 session 才生效(可接受)

---

## ADR-007:任务 worker 执行机制 = Kanban worker(durable)

**状态:** Accepted

**决策:**
社区任务通过 Kanban 系统执行:
- 收到社区 task webhook → `kanban_create(assignee_profile="nurture-task-worker")`
- Kanban dispatcher 自动 spawn worker hermes 实例
- worker 加载 nurture plugin + per-app persona,执行任务
- worker 用 `kanban_complete(outcome=...)` 结束 → connector 转发 `nurture.task.complete` 到社区

**替代方案被拒(delegate_task 同步 subagent):**
- 同步阻塞主 agent,无法并发
- 失去 Kanban 原生的 reclaim/heartbeat/重试

**后果:**
- 多设备场景天然友好(每设备一个 worker profile)
- OpenClaw 手写的 sweepStaleTaskSessions 等 ~150 LOC 全部省掉
- 需要 Phase 0 阶段预设 nurture-task-worker profile

---

## 决策汇总

| ADR | 主题 | 决策 | 影响 Phase |
|---|---|---|---|
| 001 | 协议层 | MCP(同步查询)+ HTTP polling(批量上下行) | 3 |
| 002 | 存储后端 | PG + JSON 双后端 | 2 |
| 003 | Layer 2 | MVP 仅打通管线 | 5 |
| 004 | 迁移 | 纯文件复制 | 6 |
| 005 | Recipe vs Skill | 完全独立(保守) | 0, 1, 6 |
| 006 | Persona/Goals | 独立文件 + pre_llm_call | 0, 1 |
| 007 | Task worker | Kanban worker | 4 |
