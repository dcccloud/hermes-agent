# 设备侧 Agent — Hermes Plugin Delta

**前置阅读:** OpenClaw [device-agent.md](https://github.com/openclaw/openclaw/blob/main/docs/avatar-community/device-agent.md)。本文档只列与 OpenClaw 不同的部分。

---

## 1. 形态变化

| 维度 | OpenClaw | Avatar-Hermes |
|---|---|---|
| 框架宿主 | OpenClaw 实例 + extensions/nurture | Hermes 实例 + plugins/nurture |
| 入口语言 | TypeScript | Python |
| 跨语言桥接 | TS extension ↔ Python service | Hermes plugin (Python) ↔ Python service |
| 设备工具暴露 | OpenClaw `nodes` 工具 | Hermes `register_tool` 注册的 `nurture_execute` |
| 持久化路径 | `OPENCLAW_HOME/nurture-workspaces/` | `~/.hermes/nurture/`(profile-aware) |

---

## 2. 不变内容(从 OpenClaw 完整保留)

- **自进化 RPA 引擎**:AdaptiveStep / RecipeGenerator / RecipeReviewer / AppGraph / OperationNamer 全部保留
- **5 个核心组件**:`learning/app_graph.py` / `learning/adaptive_step.py` / `learning/recipe_generator.py` / `learning/recipe_reviewer.py` / `learning/operation_namer.py`
- **Recipe 生命周期**:生成 → 候选 → 晋升 → 备份 → 自动禁用 → 自动 rollback
- **数据采集能力**:douyin.scrape_overview / fan_analysis / content_analysis / view_my_profile
- **VLM 兜底**:vision_utils.py + persona_context.py
- **拟人化**:humanize.py
- **Persona/Goals 系统**:见 [decisions.md ADR-006](decisions.md#adr-006)

---

## 3. 关键 delta

### 3.1 工具暴露方式

**OpenClaw:** Pi Agent 通过 `nodes` 工具调 `nurture.execute`,经 OpenClaw Gateway WebSocket 路由到 Python node。

**Avatar-Hermes:** 两类工具来源:
1. **本地设备工具**(`ctx.register_tool` 注册):`nurture_execute` / `nurture_task_report` — 工具 handler 用 httpx 调本地 :8600
2. **远端社区工具**(`register_mcp_servers` 注册的 MCP server 自动发现):`nurture.recipes.get` / `nurture.graph.get` / `nurture.event.query` 等 — Pi Agent 自动看到

```python
# plugins/nurture/device_tools.py
from tools.registry import registry

def nurture_execute(operation: str, params: dict = None, task_id: str = None) -> str:
    bridge = get_device_bridge()
    result = bridge.execute(operation=operation, params=params or {})
    return json.dumps(result)

registry.register(
    name="nurture_execute",
    toolset="nurture",
    schema={
        "name": "nurture_execute",
        "description": "Execute a device operation. ...",
        "parameters": {...},
    },
    handler=lambda args, **kw: nurture_execute(
        operation=args.get("operation", ""),
        params=args.get("params"),
        task_id=kw.get("task_id"),
    ),
    check_fn=lambda: device_service_running(),
    requires_env=[],
)
```

**对 LLM 的可见性完全一致** — 本地工具 schema / 描述 / 调用方式与 OpenClaw 等价;社区工具通过 MCP 自动发现(跟 OpenClaw 通过 `nodes` 工具发现远端 nurture.* 体验对等)。

### 3.2 Persona/Goals 注入

**OpenClaw:** `before_prompt_build` hook 在 system prompt 加 XML 块。

**Avatar-Hermes:** `pre_llm_call` hook(Hermes 文档明确推荐的 system prompt 注入点)。

```python
# plugins/nurture/prompt_hook.py
def create_persona_injection_hook(bridge, config):
    def hook(task_id, session_id, user_message, conversation_history, **kwargs):
        # 幂等检查 — 同 session 内只注入一次(prompt caching 友好)
        if has_injected_in_session(session_id):
            return None

        caps = bridge.get_capabilities()
        persona_xml = build_persona_xml(bridge, caps.apps)
        goals_xml = build_goals_xml(bridge)
        caps_xml = build_caps_xml(caps)

        mark_injected(session_id)
        return {
            "extra_system_prompt": f"\n{caps_xml}\n{persona_xml}\n{goals_xml}\n"
        }
    return hook
```

### 3.3 Python 服务子进程管理

**OpenClaw:** 通过 `api.registerService(...)` 让 framework 管 lifecycle。

**Avatar-Hermes:** Hermes 没有等价 API([plugin-api-audit.md](plugin-api-audit.md) 缺口 4)。变通方案:

```python
# plugins/nurture/service.py
import subprocess
import atexit
import os

_python_proc = None

def start_python_service(config):
    global _python_proc
    if _python_proc and _python_proc.poll() is None:
        return                                 # 已起,幂等
    env = os.environ.copy()
    env["NURTURE_WORKSPACE"] = str(config.workspace)
    _python_proc = subprocess.Popen(
        [config.python_path, "-m", "uvicorn", "server:app",
         "--host", config.server_host, "--port", str(config.server_port)],
        cwd=str(plugin_python_dir()),
        env=env,
    )
    atexit.register(_stop_python_service)

def _stop_python_service():
    global _python_proc
    if _python_proc:
        _python_proc.terminate()
        try:
            _python_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _python_proc.kill()
```

挂在 `on_session_start` hook 里调 `start_python_service`(进程级 singleton,多 session 共享一个 :8600)。

### 3.4 社区通信(community connector)

**OpenClaw:** TS `community-connector.ts`,启动 4 个 setInterval。

**Avatar-Hermes:** `community_sync.py` + `community_poll.py`,起一个 background thread 跑 6 个周期任务(详见 [http-protocol.md](http-protocol.md))。

### 3.5 任务执行(community task)

**OpenClaw:** `subagent.run({sessionKey: "community-task:<id>"})`。

**Avatar-Hermes:** `kanban_create()` + worker profile,详见 [task-system.md](task-system.md) 与 [decisions.md ADR-007](decisions.md#adr-007)。

---

## 4. 文件路径迁移

| OpenClaw 路径 | Avatar-Hermes 路径 |
|---|---|
| `OPENCLAW_HOME/nurture-workspaces/agent/goals.md` | `~/.hermes/nurture/agent/goals.md` |
| `OPENCLAW_HOME/nurture-workspaces/agent/persona/<app>.md` | `~/.hermes/nurture/agent/persona/<app>.md` |
| `OPENCLAW_HOME/nurture-workspaces/agent/community-tasks.json` | `~/.hermes/nurture/agent/community-tasks.json` |
| `OPENCLAW_HOME/nurture-workspaces/agent/community-recipes-cache.json` | `~/.hermes/nurture/agent/community-recipes-cache.json` |
| `OPENCLAW_HOME/nurture-workspaces/data/recipes/<app>/<op>/<step>/main.py` | `~/.hermes/nurture/data/recipes/<app>/<op>/<step>/main.py` |
| `OPENCLAW_HOME/nurture-workspaces/data/app_graphs/<app>.json` | `~/.hermes/nurture/data/app_graphs/<app>.json` |
| `OPENCLAW_HOME/nurture-workspaces/data/traces/<app>/<op>.jsonl` | `~/.hermes/nurture/data/traces/<app>/<op>.jsonl` |
| `OPENCLAW_HOME/history/profiles/<account>/*` | `~/.hermes/nurture/history/profiles/<account>/*` |

**Schema 完全不变**(包括 meta.json),`hermes nurture migrate` 是纯路径复制。

---

## 5. 离线/在线行为

| 配置 | 行为 |
|---|---|
| `community.url: ""` | 完全离线,Pi Agent + 本地 Recipe 学习正常工作 |
| `community.url: "https://..."` | background thread 启动,双向同步 |
| 社区不可达 | background thread 静默重试,本地操作不受影响 |

---

## 6. 实现状态(目标)

Phase 1 完成时,以下从 OpenClaw 完整继承,**功能对等**:

- ✓ Seed 状态图(douyin)
- ✓ Seed Operations(enter_data_center, give_a_like, ... 全套)
- ✓ 自动状态发现
- ✓ 自动 Recipe 生成
- ✓ 截图 perceptual hash + indicator 二次确认
- ✓ Tap-to-pause 重试
- ✓ LLM 复盘 + candidate
- ✓ 晋升 / 回滚
- ✓ 6 个 Recipe 质量指标
- ✓ OperationNamer
- ✓ 数据采集
- ✓ Community Connector(改 HTTP)

未实现(同 OpenClaw):
- ✗ 引导式学习目标输入
- ✗ 主动探索模式
- ✗ 跨 APP 迁移
