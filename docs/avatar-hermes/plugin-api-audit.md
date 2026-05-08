# Hermes 插件 API 表面审计

为 `plugins/nurture` 和 `plugins/nurture-community` 实现做的前置审计。结论决定 register(ctx) 该写什么、哪些计划中的 API 不可用、需要什么变通。

审计日期:2026-05-07
审计代码版本:hermes-agent main (clone 自 NousResearch/hermes-agent)

---

## 总览表

| 类别 | API | 存在? | 文件 | 备注 |
|---|---|---|---|---|
| 工具 | `ctx.register_tool(...)` | ✓ | `hermes_cli/plugins.py:242` | 委托 `tools/registry` |
| 钩子 | `ctx.register_hook(name, cb)` | ✓ | `hermes_cli/plugins.py:78-114` | 15 个有效 hook |
| 钩子 | `pre_llm_call` | ✓ | 同上 | ★ 注入 persona/goals 用 |
| 钩子 | `on_session_start / on_session_end` | ✓ | 同上 | 服务 spawn 模式靠这两个 |
| 钩子 | `pre_tool_call / post_tool_call` | ✓ | 同上 | 可 block 工具调用 |
| 钩子 | `post_llm_call` | ✓ | 同上 | 收到 assistant_message |
| CLI | `ctx.register_cli_command(...)` | ⚠ 部分 | `hermes_cli/plugins.py:301` | API 存在但 argparse wiring 只覆盖 memory 插件;通用插件的 CLI 命令注册到 `_cli_commands` 字典但 `hermes_cli/main.py:9754` 只读 `plugins.memory.discover_plugin_cli_commands`。`hermes meet` 等也受影响。详见下方"缺口 0" |
| 服务 | `ctx.register_service(...)` | ✗ | — | 用 on_session_start/end 模式替代 |
| Cron | `ctx.register_cron_job(...)` | ✗ | — | **缺口**,用 background thread 替代 |
| Webhook | 插件级 webhook 路由注册 | ✗ | — | **缺口**,改 HTTP polling 或 plugin 自起 server |
| MCP | 插件注册 MCP server / external URL | ✓ | `tools/mcp_tool.py:2825` | `register_mcp_servers(dict)` 公开 API,plugin 可调 |
| 路径 | `get_hermes_home()` | ✓ | `hermes_constants.py:14` | 安全 import |
| 投递 | `ctx.dispatch_tool("send_message", ...)` | ✓ | `tools/send_message_tool.py` | 跨平台投递通过工具分发 |
| 配置 | `ctx.config` | ✗ | — | 用 `load_config()` + `cfg_get()` 手动读 |
| Curator | `curator_skip_filter` | ✗ | — | B 方案不需要,记录 |

---

## 关键缺口详细分析

### 缺口 0:通用插件 CLI 命令未 wire 到 argparse(Phase 0 验证时发现)

**计划用途**:`hermes nurture status` / `hermes nurture-community status` 等 CLI 子命令。

**Hermes 现状**:
- `ctx.register_cli_command(...)` 把命令存入 `manager._cli_commands` 字典
- `hermes_cli/main.py:9754` 只通过 `plugins.memory.discover_plugin_cli_commands()` 读取 memory 插件的 CLI
- **通用插件(包括 google_meet 的 `hermes meet`)的 CLI 命令永远不会被加进 main argparse**
- 影响范围:所有 standalone kind 插件的 CLI 命令

**症状(实测)**:

```bash
$ hermes plugins enable nurture
✓ Plugin nurture enabled.

$ hermes nurture status
hermes: error: argument command: invalid choice: 'nurture' (...)

# google_meet 同样:
$ hermes meet status
hermes: error: argument command: invalid choice: 'meet' (...)
```

**变通方案**:

A. **在 plugin 内同时注册 slash command**(`ctx.register_command`)— hermes session 内可用 `/nurture-status` 触发,不需要 CLI argparse 集成
B. **写一个独立的 `hermes-nurture` shell wrapper** — 直接调 plugin 的 handler_fn,绕开 hermes argparse
C. **向 hermes 上游提 PR**:加 `discover_general_plugin_cli_commands` 类似 memory 插件路径,把通用插件 CLI 一并 wire

**推荐 A + C 并行:**
- A 立即可用,Phase 0 commit 时一起加(slash command 是稳定的备选路径)
- C 是真正的修复,影响所有 standalone 插件,值得提 PR(google_meet 团队也是受益者)

**对 Phase 0 影响**:`hermes nurture status` 暂时不能跑,但 plugin loads OK,所有 register_* 调用都执行。Phase 1+ 的工具注册(走 `discover_builtin_tools`)不受此影响 — Pi Agent 看到 `nurture_execute` 工具是正常工作的。

---

### 缺口 1:plugin-internal cron registration (E.13)

**计划用途**:nurture plugin 启动时自动注册 4 个内部周期任务(capability 60s / trace 60s / event 60s / task poll 30s),不依赖用户通过 cronjob 工具创建。

**Hermes 现状**:
- `cron/jobs.py` + `cron/scheduler.py` 是用户级 cron,持久化在 `~/.hermes/cron/jobs.json`
- 插件无法在 register(ctx) 里调 `add_job` 注册"系统级"任务
- 即使用 `dispatch_tool("cronjob", ...)` 也会污染用户的 cron 列表

**变通方案**:**background thread + asyncio.Event**

```python
# plugins/nurture/__init__.py
import threading
import asyncio

def register(ctx):
    stop_event = threading.Event()
    config = ...

    def cron_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        last_caps = 0
        last_trace = 0
        last_event = 0
        last_task = 0
        while not stop_event.is_set():
            now = time.time()
            if now - last_caps >= 60:
                loop.run_until_complete(report_capabilities(config))
                last_caps = now
            if now - last_trace >= 60:
                loop.run_until_complete(report_traces(config))
                last_trace = now
            # ... etc
            stop_event.wait(timeout=5)  # tick every 5s

    thread = threading.Thread(target=cron_loop, daemon=True, name="nurture-cron")
    thread.start()
    ctx.register_hook("on_session_end", lambda **kw: stop_event.set())
```

**风险**:Hermes 是 long-running 进程(尤其 gateway 模式),thread 应该和进程同生共死。`on_session_end` 是 per-session 触发,可能不是我们要的语义 — 需要在实现时确认。

### 缺口 2:plugin webhook route registration (F.17)

**计划用途**:社区主动 push task.dispatch / advice / directive 到设备,设备插件接收后写本地 store。

**Hermes 现状**:
- `hermes_cli/webhook.py` 提供 `hermes webhook subscribe` 用户级订阅
- 路由是预定义在 `gateway/platforms/webhook.py` 里的 — 进 LLM agent loop 触发新一轮对话
- **没有让插件在 gateway 上加自定义 HTTP route 的 API**

**变通方案三选一**:

A. **改 HTTP polling**(★ 推荐):cron 也轮询 task/advice/directive 三类事件,丢弃 push。30s 间隔够实时。Phase 3 协议层全部退化为 cron + HTTP polling,简化协议工作量
B. **plugin 自起 HTTP listener**:nurture 在 on_session_start 里 spawn 一个独立 FastAPI 端口(比如 :18791)接收 webhook,完全脱离 hermes gateway。HMAC 自己实现
C. **借用现有 webhook 机制 + pre_tool_call**:用户手动 `hermes webhook subscribe nurture-inbox`,触发后进 LLM 对话,nurture 用 pre_tool_call 拦截 — 重度 hack,不推荐

**推荐 A**。失去推送实时性,但避开了所有架构问题。

### 缺口 3 → 实际可用:plugin MCP server registration (G.19, G.20)

**修订(2026-05-07):** 初版 audit 错误地将此项标记为缺口。复查后确认 **`tools/mcp_tool.py:2825` 暴露 `register_mcp_servers(servers: Dict[str, dict]) -> List[str]` 公开 API**,plugin 可在 `register(ctx)` 里直接调用注册外部 MCP server。

**Hermes 实际能力(完整列表):**
- ✓ MCP server (`mcp_serve.py`):暴露聊天会话给外部 MCP client
- ✓ MCP client (`tools/mcp_tool.py`):连外部 MCP server,discover 工具,注册到 hermes 工具表
- ✓ **`register_mcp_servers(dict)` 公开 API** — plugin 可程序化调用
- ✓ Stdio + HTTP/StreamableHTTP 传输
- ✓ MCP OAuth(`mcp_oauth.py` + `mcp_oauth_manager.py`)
- ✓ Sampling 支持(MCP server 反向请求 LLM)
- ✓ Tool 过滤(include/exclude)
- ✓ config.yaml mcp_servers 段变更自动 reload

**plugin 用法:**

```python
# plugins/nurture/__init__.py
from tools.mcp_tool import register_mcp_servers

def register(ctx):
    config = ...
    if config.community.url:
        register_mcp_servers({
            "nurture-community": {
                "url": f"{config.community.url}/mcp",
                "headers": {
                    "Authorization": f"Bearer {load_token()}",
                },
                "timeout": 30,
                "connect_timeout": 10,
            }
        })
        # Pi Agent 自动看到社区暴露的 nurture.* 工具
```

**结论:** MCP 是 Phase 3 的可行路径。详见 [decisions.md ADR-001](decisions.md#adr-001)。

### 缺口 4:register_service (C.9)

**计划用途**:nurture plugin 启动时拉起 Python 子进程(:8600 FastAPI),hermes 关时杀掉。

**变通方案**:on_session_start 里 spawn(配合幂等检查 — 已起则不重 spawn),atexit 钩子退出时 kill。语义上接受多个 session 共享一个 Python service 进程。

```python
# plugins/nurture/service.py
import atexit
import subprocess

_python_proc = None

def start_python_service(config):
    global _python_proc
    if _python_proc and _python_proc.poll() is None:
        return  # already running
    _python_proc = subprocess.Popen([config.python_path, "server.py", ...])
    atexit.register(stop_python_service)

def stop_python_service():
    global _python_proc
    if _python_proc:
        _python_proc.terminate()
        _python_proc.wait(timeout=5)
```

---

## Phase 3 协议层的设计修订

基于上面缺口的变通方案 + MCP 重新可用,Phase 3 定义为 **MCP + HTTP polling**:

```
┌─ 设备侧 hermes plugin ─────────────────────────┐
│  register(ctx):                                │
│    register_mcp_servers({                      │
│      "nurture-community": {                    │
│        "url": "https://.../mcp",               │
│        "headers": {                            │
│          "Authorization": "Bearer <token>"     │
│        }                                       │
│      }                                         │
│    })                                          │
│  → Pi Agent 自动发现 nurture.* 工具(同步查询)   │
│                                                │
│  background thread (替代 cron job 注册):        │
│    上行 (HTTP POST /api/upload):                │
│    ├─ 60s: report capability                   │
│    ├─ 60s: report traces                       │
│    └─ 60s: report events                       │
│    下行 (HTTP GET, polling 替代 webhook push):   │
│    ├─ 30s: GET /api/tasks/poll                 │
│    ├─ 30s: GET /api/advices/poll               │
│    └─ 30s: GET /api/directives/poll            │
└────────────────────────────────────────────────┘
            │ MCP HTTP + HTTP/JSON,no Websocket
            ▼
┌─ 社区侧 hermes plugin ─────────────────────────┐
│  on_session_start: 起 FastAPI :18790           │
│                                                │
│  MCP server endpoint (基于 mcp_serve.py 模板):   │
│    └─ /mcp  (StreamableHTTP transport)         │
│        暴露:nurture.recipes.get / graph.get /  │
│              event.query / ...                 │
│                                                │
│  REST endpoints (上下行批量):                    │
│    ├─ POST /api/upload          (capability/trace/event)
│    ├─ GET  /api/tasks/poll      (avatarId-scoped)
│    ├─ GET  /api/advices/poll    (agentId-scoped)
│    ├─ GET  /api/directives/poll (avatarId-scoped)
│    └─ POST /api/task/complete                  │
│                                                │
│  Bearer token auth (token bind avatarId,       │
│    既校验 MCP HTTP headers,也校验 REST Auth)    │
└────────────────────────────────────────────────┘
```

**对计划的影响:**

| Phase 3 原方案 (MCP+Webhook+Cron) | 修订后 (MCP + HTTP polling) | 工作量变化 |
|---|---|---|
| MCP client 注册 | 保留(`register_mcp_servers` 一行调用) | ±0 |
| MCP server (社区侧) | 保留(基于 `mcp_serve.py` 模板) | ±0 |
| Webhook push 下行 | 改 HTTP polling | -0.4d |
| 注册 3 个 polling cron | 新增 | +0.3d |
| HTTP wrapper 工具 | 取消(MCP 自动暴露) | -0.3d |
| **净变化** | | **-0.4d** |

Phase 3 总时长从 3.5d 降到 3.1d。**MCP 同步查询 + HTTP 批量上下行**,职责清晰。

---

## 对其他 Phase 的影响

| Phase | 是否受影响 | 备注 |
|---|---|---|
| Phase 0 | 轻微 | 加 background thread 启动逻辑;`hermes nurture list` 工具替代 hermes skills 可见性 |
| Phase 1 | 无 | 设备 Python lift 与 protocol 无关 |
| Phase 2 | 无 | store 翻译只跟数据模型相关 |
| **Phase 3** | **重大调整** | 见上,改 HTTP polling + HTTP tools |
| Phase 4 | 无 | Kanban 走 hermes 原生工具 |
| Phase 5 | 轻微 | analytics 用现有 cron + send_message tool 即可 |
| Phase 6 | 无 | 迁移工具不依赖协议层 |

---

## 是否向 hermes 上游提 PR?

短期(MVP)走变通方案不提 PR。中期可以考虑提以下两个 PR:

1. **`ctx.register_cron_job(name, schedule, handler)`**:让插件注册系统级周期任务,不污染用户 cron list
2. **`ctx.register_webhook_route(path, handler, auth=...)`**:让插件在 gateway 上加自定义 HTTP route(目前下行只能 polling)

MCP 不再列入 PR 列表 — `register_mcp_servers()` 已经够用。

这些 API 不仅 nurture 用得上,其他需要"长期运行后台服务"的插件(如 observability, kanban dispatcher)也用得上。文档里 "plugins MUST NOT modify core" 的精神也是鼓励通过加通用 hook 而不是塞特定逻辑。

短期不阻塞,长期可以推动。

---

## 结论

**没有阻塞性问题**。两个核心缺口(cron registration / webhook route)有可行的纯插件变通方案(background thread + HTTP polling)。MCP 经复查可用,plugin 可调 `register_mcp_servers()` 注册外部 MCP server。

Phase 3 协议层定为 **MCP(同步查询)+ HTTP polling(批量上下行)+ background thread(定时器)**。Pi Agent 通过 MCP 自动发现远端工具,与 OpenClaw 原版 nodes 体验对等。

可以推进 Phase 0 (脚手架)。
