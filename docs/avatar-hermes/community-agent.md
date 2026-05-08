# 社区侧 Agent — Hermes Plugin Delta

**前置阅读:** OpenClaw [community-agent.md](https://github.com/openclaw/openclaw/blob/main/docs/avatar-community/community-agent.md)。本文档只列与 OpenClaw 不同的部分。

---

## 1. 形态变化

| 维度 | OpenClaw | Avatar-Hermes |
|---|---|---|
| 宿主 | OpenClaw 实例 + extensions/nurture-community | Hermes 实例 + plugins/nurture-community |
| 入口语言 | TypeScript | Python |
| 对外接口 | OpenClaw Gateway 注册的 `nurture.*` 方法 | FastAPI HTTP endpoints (:18790) |
| 数据库后端 | PG (asyncpg) / JSON | PG (asyncpg) / JSON ✓ 对齐 |

---

## 2. 不变内容

完整继承 OpenClaw community-agent.md 的所有概念:

- **知识进化引擎**(第一层加速):Recipe 分发 / Graph 共识 / Recipe 融合
- **APP 频道隔离**:按 app 维度隔离
- **策略智能引擎**(第二层 — Phase 5 MVP):analytics 聚合 + advice/directive
- **Agent 注册与管理**:AvatarRegistry,30s reconcile

---

## 3. 关键 delta

### 3.1 通信协议

**OpenClaw:** `nurture.*` Gateway methods (WebSocket node 协议)。

**Avatar-Hermes:** FastAPI HTTP endpoints,详见 [http-protocol.md](http-protocol.md)。

### 3.2 入口结构

```python
# plugins/nurture-community/__init__.py
import asyncio
from .community_server import build_app
from .knowledge_engine import KnowledgeEngine

def register(ctx):
    config = load_config()
    plugin_cfg = cfg_get(config, "plugins", "nurture-community", default={})
    if not plugin_cfg.get("enabled", False):
        return

    engine = KnowledgeEngine(database_url=plugin_cfg.get("databaseUrl"))
    app = build_app(engine, plugin_cfg)

    # 起 FastAPI 服务(uvicorn,后台线程)
    def start_server():
        import uvicorn
        config = uvicorn.Config(
            app=app,
            host=plugin_cfg.get("host", "127.0.0.1"),
            port=plugin_cfg.get("port", 18790),
            log_level="info",
        )
        server = uvicorn.Server(config)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(engine.init())   # 跑 migration.sql
        loop.run_until_complete(server.serve())

    import threading
    thread = threading.Thread(target=start_server, daemon=True, name="nurture-community-server")
    thread.start()

    # CLI 子命令
    ctx.register_cli_command(
        name="nurture-community",
        help="Avatar-Hermes community management",
        setup_fn=cli.build_parser,
    )
```

### 3.3 Recipe 融合 LLM 调用

**OpenClaw:** 用 OpenClaw 自己的 LLM client。

**Avatar-Hermes:** 用 hermes `auxiliary_client`(可独立配 cheap model,降本):

```python
# plugins/nurture-community/recipe_fusion.py
from agent.auxiliary_client import get_auxiliary_client

async def fuse_recipes(candidates: list[dict]) -> str:
    client = get_auxiliary_client("recipe_fusion")  # 从 config 读 task-specific provider
    prompt = build_fusion_prompt(candidates)
    response = await client.chat.completions.create(
        model=client.model,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
```

`auxiliary_client` 配置在 `~/.hermes/config.yaml` 的 `auxiliary.recipe_fusion` 段(可独立指定 provider/model/api_key)。

### 3.4 Phase 5 — 多平台投递(优于 OpenClaw)

OpenClaw 没有内置投递能力。Avatar-Hermes 借 hermes 原生:

```python
# plugins/nurture-community/analytics/weekly_report.py
async def send_weekly_report():
    report = await build_report()
    # hermes 的 send_message 工具支持 13+ 平台
    await dispatch_tool("send_message", {
        "platform": "telegram",
        "target": cfg_get(config, "plugins", "nurture-community", "report_target"),
        "text": report,
    })
```

通过 hermes cron job 每周触发(不是插件内部 cron — 周报性质适合用户级 cron):

```bash
hermes cron create "0 9 * * 1" \
  "Generate Avatar-Hermes weekly report" \
  --script ~/.hermes/scripts/nurture-weekly-report.py \
  --deliver telegram \
  --name "Nurture weekly report"
```

---

## 4. 文件路径

| OpenClaw 路径 | Avatar-Hermes 路径 |
|---|---|
| `nurture-workspaces/community/knowledge/recipes.json` | `~/.hermes/nurture-community/knowledge/recipes.json` |
| `nurture-workspaces/community/knowledge/graph-states.json` | `~/.hermes/nurture-community/knowledge/graph-states.json` |
| 同上 events / traces / advices / directives / task-dispatch / value-schema | 同上 |

PG 模式下,所有数据在数据库,这里只是 JSON 模式的本地文件。

---

## 5. 实现状态(目标)

Phase 2 完成时,以下从 OpenClaw 翻译完成、**功能对等**:

- ✓ Recipe 上报/分发/融合/共识
- ✓ KnowledgeEngine 双后端
- ✓ 7 大 store + PG 实现
- ✓ AvatarRegistry
- ✓ AdviceEngine
- ✓ DirectiveEngine
- ✓ TaskDispatch
- ✓ Event/Trace 存储

Phase 5 完成时,新增(超越 OpenClaw):

- ✓ analytics 离线聚合
- ✓ 周报多平台投递
- ✓ Langfuse trace 可视化
- ✓ 1 个示例 advice 规则
- ✓ 1 个示例 directive

未实现(同 OpenClaw,本次不背):
- ✗ 完整 A/B 实验框架
- ✗ 跨 APP 迁移
- ✗ Agent 信任评级
