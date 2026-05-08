# 设备侧记忆与进化 — Hermes Plugin Delta

**前置阅读:** OpenClaw [device-memory-evolution.md](https://github.com/openclaw/openclaw/blob/main/docs/avatar-community/device-memory-evolution.md)。本文档只列与 OpenClaw 不同的部分。

---

## 1. 不变内容(完整保留)

OpenClaw 文档里描述的所有记忆机制 **schema 与逻辑 1:1 保留**:

- TraceStore / RecipeStore / AppGraph / EventLog 四子系统
- Recipe 生命周期 8 个阶段(生成 → 候选 → 晋升 → 备份 → 自动禁用 → 自动 rollback → 社区同步 → 反哺社区)
- Recipe `meta.json` 全部字段(success/failure/streak/version/source/community_*/denied_*/rolled_back_from)
- AdaptiveStep 三层执行(Tier 0/1/2)
- StateDiscovery 自动页面发现
- RecipeGenerator / RecipeReviewer
- Humanize API
- 社区 Recipe Cache + on-execute 同步

**所有 Python 代码从 `extensions/nurture/python/openclaw_agent/` 整体 lift,不做重构。**

---

## 2. 唯一 delta:存储路径

**所有记忆体落地路径迁移到 `~/.hermes/nurture/data/` 下:**

```
~/.hermes/nurture/                            # NURTURE_WORKSPACE
├── data/
│   ├── recipes/<app>/<operation>/<step>/
│   │   ├── main.py
│   │   ├── main.meta.json                    # ← schema 与 OpenClaw 1:1
│   │   ├── main.candidate.py + .meta.json
│   │   └── main.previous.py + .meta.json
│   ├── app_graphs/<app>.json
│   ├── traces/<app>/<operation>.jsonl
│   ├── events/                               # cursor-based pending events
│   ├── checkpoints/                          # 任务断点
│   └── in_flight/                            # 正在进行的任务状态
└── agent/
    ├── goals.md
    ├── persona/<app>.md
    ├── community-tasks.json
    ├── community-recipes-cache.json
    ├── community-token.json                  # ★ 新增:Bearer token 缓存
    ├── value-schema.json
    ├── advice.json
    └── directives.json
```

---

## 3. 与 Hermes Skills 系统的关系

**完全独立**(详见 [decisions.md ADR-005](decisions.md#adr-005)):

- `~/.hermes/skills/` 不放任何 nurture 资产
- Hermes Skills loader 不扫描 nurture 目录
- Hermes Curator 不接触 nurture recipes
- AdaptiveStep **不 import** `hermes.skills` 任何模块
- `hermes skills list` 看不到 recipe;改用 `hermes nurture list`

---

## 4. NURTURE_WORKSPACE 环境变量

Python 设备服务通过 `NURTURE_WORKSPACE` 环境变量定位数据目录。Hermes plugin 启动 Python 子进程时:

```python
env["NURTURE_WORKSPACE"] = str(get_hermes_home() / "nurture")
```

支持 hermes profile 隔离 — 每个 profile 独立的 nurture 工作区。

---

## 5. 性能基线

延续 [boundary-contracts.md 不变量 1](boundary-contracts.md#不变量-1) 的性能 SLO:

| 指标 | 目标 |
|---|---|
| Recipe 加载到开始执行 | < 5ms |
| 单步 recipe 执行 | < 200ms (typical) |
| operation 端到端 vs OpenClaw | 差异 < 5% |
| warm path LLM call 数 | == 1 |
| 社区 recipe 同步开销 | < 1ms (mtime check) |

---

## 6. 测试

Phase 1 验收依据:

- ✓ pytest fixture 跑通完整 Recipe 生命周期(生成 → 候选 → 晋升 → rollback → 社区同步)
- ✓ AdaptiveStep 在有 recipe 场景下整步骤执行 0 LLM call
- ✓ `hermes nurture migrate` 能把 OpenClaw recipes 迁移过来,所有 meta.json 字段无损
