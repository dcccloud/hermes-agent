# 社区侧记忆与进化 — Hermes Plugin Delta

**前置阅读:** OpenClaw [community-memory-evolution.md](https://github.com/openclaw/openclaw/blob/main/docs/avatar-community/community-memory-evolution.md)。本文档只列与 OpenClaw 不同的部分。

---

## 1. 不变内容(完整保留)

- **7 大 store + 1 个模型存储**:RecipeStore / GraphConsensus / RecipeFusion / EventStore / TraceStore / TaskDispatch / AdviceEngine / DirectiveEngine + ValueSchema
- **双后端架构**:JSON(开发)+ PostgreSQL 17.5(生产)
- **JSON 后端循环缓冲**:events 20k / traces 10k(由翻译时显式实现)
- **PostgreSQL 表结构**:`migration.sql` 直接复用 OpenClaw 版本,**SQL 不动**
- **Recipe 反哺与自动剔除**机制
- **Graph 共识**(GRAPH_CONSENSUS_MIN=2)
- **分发触发条件**(success_rate < 全局平均 - 0.1)
- **融合触发条件**(同步骤 ≥2 版本 & 样本 ≥5)

---

## 2. 关键 delta

### 2.1 TS → Python 翻译规则

| TS 概念 | Python 等价 |
|---|---|
| `node-pg` 客户端 | `asyncpg` + `pg-pool` 连接池 |
| `Promise<T>` | `async def ... -> T` |
| `JSON.parse/stringify` | `json.loads/dumps` |
| `pg query` | `await conn.fetch(sql, *args)` |
| TS 类型 | pydantic `BaseModel` |
| `setInterval` | `asyncio.create_task` + `while True: await asyncio.sleep(...)` |
| TS test (vitest) | pytest + pytest-asyncio |

### 2.2 LLM 调用统一

**OpenClaw:** Recipe Fusion 用 OpenClaw 自带 LLM。

**Avatar-Hermes:** 用 `agent.auxiliary_client.get_auxiliary_client("recipe_fusion")`。可在 `config.yaml` 独立配置:

```yaml
auxiliary:
  recipe_fusion:
    provider: openrouter
    model: anthropic/claude-haiku
    max_tokens: 4096
    # 不影响主 agent 的 LLM 配置
```

### 2.3 PG 连接池

```python
# plugins/nurture-community/pg/pool.py
import asyncpg

_pool = None

async def get_pool(database_url: str) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=database_url,
            min_size=2,
            max_size=10,
            command_timeout=30.0,
        )
    return _pool

async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
```

参数对齐 OpenClaw `pg-pool.ts`(max=10, idle=30s, connect=5s)。

### 2.4 数据库迁移

启动时通过 `KnowledgeEngine.init()` 执行 `migration.sql`。由于使用 `IF NOT EXISTS`,幂等:

```python
async def init(self):
    if self.is_pg:
        pool = await get_pool(self.database_url)
        async with pool.acquire() as conn:
            sql = (Path(__file__).parent / "pg" / "migration.sql").read_text()
            await conn.execute(sql)
```

### 2.5 测试翻译

OpenClaw 每个 store 有 .test.ts(用 vitest)。翻译时映射:

```typescript
// OpenClaw
describe("RecipeStore", () => {
  it("ingests from capability", async () => {
    const store = new RecipeStore(...);
    await store.ingestFromCapability({...});
    expect(store.get(...)).toEqual(...);
  });
});
```

```python
# Avatar-Hermes
import pytest
from plugins.nurture_community.stores.recipe_store import RecipeStore

@pytest.mark.asyncio
async def test_ingests_from_capability(tmp_path):
    store = RecipeStore(workspace=tmp_path)
    await store.ingest_from_capability({...})
    result = await store.get(...)
    assert result == ...
```

### 2.6 进化效果指标(对照 OpenClaw 表)

完全保留。Avatar-Hermes 在以下方面**优于 OpenClaw**:

- LLM 融合成本:可独立配 cheap model(OpenClaw 不行)
- 周报 / 告警:多平台投递(OpenClaw 没有投递能力)
- 可视化:Langfuse(OpenClaw 没有)

---

## 3. 文件路径

| OpenClaw | Avatar-Hermes |
|---|---|
| `extensions/nurture-community/src/recipe-store.ts` | `plugins/nurture-community/stores/recipe_store.py` |
| `extensions/nurture-community/src/pg-recipe-store.ts` | `plugins/nurture-community/stores/pg_recipe_store.py` |
| `extensions/nurture-community/src/migration.sql` | `plugins/nurture-community/pg/migration.sql` (复用) |
| `extensions/nurture-community/src/recipe-fusion.ts` | `plugins/nurture-community/recipe_fusion.py` |
| ...(以此类推 7 大 store) | ... |

---

## 4. 数据库配置

```yaml
# ~/.hermes/config.yaml
plugins:
  nurture-community:
    enabled: true
    host: 127.0.0.1
    port: 18790
    databaseUrl: "postgresql://user:pass@host:5432/db?sslmode=require"
```

无 databaseUrl → 自动 fallback 到 JSON 后端。

---

## 5. 实现状态(目标)

Phase 2 完成时:

- ✓ 7 大 store(JSON 实现)
- ✓ 7 大 store(PG 实现)
- ✓ KnowledgeEngine 双后端门面
- ✓ Recipe Fusion(用 auxiliary_client)
- ✓ Distribution
- ✓ AvatarRegistry
- ✓ migration.sql 复用并跑通
- ✓ 所有 .test.ts 翻译为 pytest 并通过
