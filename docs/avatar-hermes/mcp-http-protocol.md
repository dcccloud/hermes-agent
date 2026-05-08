# MCP + HTTP 协议规范

本文档定义 Avatar-Hermes 设备↔社区通信协议。**替代 OpenClaw 的 [gateway-protocol.md](https://github.com/openclaw/openclaw/blob/main/docs/avatar-community/gateway-protocol.md)**。

参见 [decisions.md ADR-001](decisions.md#adr-001) 的决策原因。

---

## 1. 协议总览

```
┌─ 设备 hermes plugin ─────────────────────────────┐
│                                                  │
│  ① MCP client(同步查询):                         │
│     register_mcp_servers({                        │
│       "nurture-community": {                      │
│         "url": ".../mcp",                         │
│         "headers": {                              │
│           "Authorization": "Bearer <token>"       │
│         }                                         │
│       }                                           │
│     })                                            │
│  → Pi Agent 自动看到 nurture.* 工具               │
│                                                  │
│  ② background thread(定时器,daemon=True):       │
│     上行 HTTP POST /api/upload(60s):              │
│       capability / trace / event                  │
│     下行 HTTP GET polling(30s):                   │
│       /api/{tasks,advices,directives}/poll        │
│                                                  │
│  ③ Bearer token(每个 agent 一个 token,           │
│       社区端 bind avatarId)                       │
└──────────────────────┬───────────────────────────┘
                       │ HTTPS (生产) / HTTP (本地)
                       │ Authorization: Bearer <token>
┌──────────────────────┴───────────────────────────┐
│  社区 hermes plugin                              │
│  FastAPI :18790                                  │
│                                                  │
│  ① MCP server endpoint /mcp(StreamableHTTP):    │
│     - nurture.recipes.get                         │
│     - nurture.graph.get                           │
│     - nurture.event.query                         │
│     - nurture.task.create   (admin scope)         │
│     - nurture.directive.create (admin scope)      │
│                                                  │
│  ② REST endpoints (上下行批量):                    │
│     POST /api/upload                              │
│     GET  /api/tasks/poll                          │
│     GET  /api/advices/poll                        │
│     GET  /api/directives/poll                     │
│     POST /api/task/complete                       │
│     POST /api/avatar/register                     │
└──────────────────────────────────────────────────┘
```

**为什么是双协议:**
- MCP 适合**同步交互式查询**(Pi Agent 拿来即用,自动发现工具)
- HTTP polling 适合**批量数据流**(capability/trace 一次几十 KB,MCP 的工具调用语义不合适)
- 下行用 polling 是因为 hermes 不支持 plugin 注册 webhook 路由(MVP 阶段不提 PR)

---

## 2. 鉴权(双协议共享)

### 2.1 Token 颁发

设备首次连接时 `POST /api/avatar/register` 上报身份(deviceId + 公钥),社区返回 Bearer token。

**Token 格式:**
- JWT,RS256 签名
- payload:`{ "avatarId": "<uuid>", "agentId": "<plugin-config>", "exp": <unix-ts>, "scope": "agent" }`
- 有效期:30 天,过期前 7 天自动续签
- admin token 单独颁发(`scope: "admin"`),社区运维使用

**配置:**
- token 落在 `~/.hermes/nurture/agent/community-token.json`
- 私钥从不落地;只缓存 token

### 2.2 双协议鉴权

| 协议 | 携带方式 |
|---|---|
| MCP HTTP | `register_mcp_servers` 配置中的 `headers.Authorization` |
| REST API | 标准 `Authorization: Bearer <token>` header |

**社区端共用同一个 JWT 校验器**,从 token 解 avatarId / scope,跨协议一致。

### 2.3 Self-scoped 强制

所有 self-scoped 端点(无论 MCP tool 还是 REST endpoint)**从 token 解 avatarId,不接受请求方传**:

```python
# REST 示例
@app.get("/api/tasks/poll")
async def poll_tasks(token = Depends(verify_token)):
    avatar_id = token.payload["avatarId"]
    tasks = await store.query_tasks_for_avatar(avatar_id)

# MCP tool 示例
@mcp.tool()
async def nurture_recipes_get(app: str, operation: str, step: str, _ctx) -> dict:
    avatar_id = _ctx.token.payload["avatarId"]
    return await recipe_store.get_best(app, operation, step)
```

---

## 3. MCP 工具列表(同步查询)

社区 MCP server 暴露的 tool 命名空间:

| Tool name | scope | 用途 |
|---|---|---|
| `nurture.recipes.get` | agent | 拿某 step 的 best recipe |
| `nurture.recipes.list` | agent | 列出某 app 的 recipe 清单 |
| `nurture.graph.get` | agent | 拿共识 graph |
| `nurture.event.query` | agent | 复杂事件查询 |
| `nurture.advice.query` | agent | (备用,主要走 polling) |
| `nurture.directive.query` | agent | (备用,主要走 polling) |
| `nurture.task.create` | admin | 社区运维创建任务 |
| `nurture.directive.create` | admin | 社区运维下发指令 |

**Tool schema 例子:**

```json
{
  "name": "nurture.recipes.get",
  "description": "Get the best recipe for an app/operation/step from the community knowledge engine.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "app": {"type": "string", "description": "App name (e.g. 'douyin')"},
      "operation": {"type": "string", "description": "Operation id (e.g. 'enter_data_center')"},
      "step": {"type": "string", "default": "main"}
    },
    "required": ["app", "operation"]
  }
}
```

Pi Agent 看到的工具名是 `nurture.recipes.get`(MCP server 默认 namespace + dot 分隔)。

---

## 4. REST 端点

### 4.1 `POST /api/upload`

统一上行端点,通过 `kind` 字段区分类型。

**Request:**

```json
{
  "kind": "capability" | "trace" | "event",
  "agentId": "agent-device-a",
  "deviceId": "R5CR10XXXXX",
  "timestamp": 1762430400.123,
  "payload": { ... }
}
```

**Response (kind=capability):** 携带 bestRecipes(共识图通过 MCP `nurture.graph.get` 拉,不在响应里)

```json
{
  "ok": true,
  "bestRecipes": [
    {
      "app": "facebook",
      "operation": "facebook/like_video_1",
      "step": "main",
      "code": "...",
      "version": "node-other-cba12345",
      "globalSuccessRate": 0.95,
      "globalSampleCount": 50,
      "originNodeId": "other-agent",
      "originDeviceModel": "Pixel-7"
    }
  ]
}
```

**Response (kind=trace, kind=event):**

```json
{ "ok": true, "ingested": 23, "deduped": 2 }
```

### 4.2 `GET /api/tasks/poll`

**Request:**
- 无 query 参数(avatarId 从 token 解出)
- Header: `If-None-Match: <etag>` (可选)

**Response (200):**

```json
{
  "tasks": [
    {
      "taskId": "task-1762430400-1",
      "app": "douyin",
      "description": "采集抖音数据中心",
      "priority": "normal",
      "expiresAt": "2026-05-08T10:00:00Z",
      "createdAt": 1762430400000
    }
  ],
  "etag": "abc123"
}
```

**Response (304):** 空 body — 没有新任务

### 4.3 `GET /api/advices/poll` / `GET /api/directives/poll`

同上,返回未送达给该 avatarId 的 advice/directive。

### 4.4 `POST /api/task/complete`

```json
// Request
{
  "taskId": "task-1762430400-1",
  "outcome": "success" | "failed" | "refused" | "partial",
  "reason": "string",
  "summary": "string (可选)"
}

// Response
{ "ok": true, "deactivated": true }
```

### 4.5 `POST /api/avatar/register`

```json
// Request
{
  "deviceId": "R5CR10XXXXX",
  "agentId": "agent-device-a",
  "publicKey": "<ed25519-pub>",
  "platform": "android"
}

// Response
{
  "avatarId": "<uuid>",
  "token": "<jwt>",
  "expiresAt": 1764030400
}
```

---

## 5. 错误码

| HTTP / MCP error | 含义 | 客户端行为 |
|---|---|---|
| 401 / Unauthorized | token 缺失/过期 | 触发续签或重新注册 |
| 403 / Forbidden | scope 不足或 avatarId 不匹配 | 不重试 |
| 404 / NotFound | 资源不存在 | 上层 fallback |
| 409 / Conflict | 重复 ingestion | 视为成功 |
| 429 / RateLimit | 限流 | 指数退避重试 |
| 5xx / InternalError | 服务端错误 | 指数退避重试 |

MCP 错误通过 JSON-RPC `error` 字段返回,code 用 -32xxx,详情在 `data` 里。

---

## 6. 重试与超时

### 设备侧

```python
# httpx client(REST 用)
client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0, connect=5.0),
    transport=httpx.AsyncHTTPTransport(retries=3),
)

# MCP client(由 hermes mcp_tool 管理)
# 自动重连 + exponential backoff,最多 5 次重试
# 单工具调用超时 120s(可配)
```

**应用层重试:**
- 上行(REST POST):失败入本地 retry queue,下次 background tick 重试
- 下行 polling(REST GET):失败跳过本轮,下次 tick 再试
- MCP tool 调用:失败直接返回错误给 LLM(让它决定 fallback)

### 社区侧

- FastAPI 默认超时 30s
- recipe-fusion / analytics 等长任务用 background task 不阻塞 HTTP
- MCP server 单次工具调用同样 30s 内返回

---

## 7. 与 OpenClaw 协议的对照

| OpenClaw (WS node 协议) | Avatar-Hermes | 备注 |
|---|---|---|
| `nurture.capability.update` event | `POST /api/upload` (kind=capability) | response 携带 bestRecipes |
| `nurture.trace.report` event | `POST /api/upload` (kind=trace) | |
| `nurture.event.report` event | `POST /api/upload` (kind=event) | |
| `nurture.task.query` invoke | `GET /api/tasks/poll` | self-scoped via token,30s polling |
| `nurture.task.complete` invoke | `POST /api/task/complete` | |
| `nurture.task.dispatch` event (push) | `GET /api/tasks/poll` (pull, 30s) | **失去推送实时性,30s 延迟** |
| `nurture.advice.push` event | `GET /api/advices/poll` (pull) | |
| `nurture.directive.push` event | `GET /api/directives/poll` (pull) | |
| `nurture.recipes.get` invoke | **MCP `nurture.recipes.get`** | ★ Pi Agent 自动发现 |
| `nurture.graph.get` invoke | **MCP `nurture.graph.get`** | ★ |
| `nurture.event.query` invoke | **MCP `nurture.event.query`** | ★ |
| `nurture.graph.merge` invoke | (内嵌在 capability upload 里) | |
| `node.invoke.async` / `node.invoke.outcome` | (不需要) | Kanban 替代异步任务管理 |

---

## 8. 未来演进

短期(MVP 不做):
- 向 hermes 上游提 `register_webhook_route` PR,提交后下行可切回 push 模式
- gRPC(性能优化,如果 trace 数据量爆炸)

长期:
- P2P 局域网直连(OpenClaw Phase 5 同样未实现)
- 端到端加密(payload 级,而非传输层)
- MCP server 在社区端用 sampling 让设备 LLM 协同推理(第二层策略生成的可能路径)
