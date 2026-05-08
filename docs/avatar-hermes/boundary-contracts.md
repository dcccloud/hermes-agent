# 系统不变量(Boundary Contracts)

本文档列出 Avatar-Hermes 系统的硬约束。任何 PR 触碰这些边界都需要 reviewer 显式批准。

---

## 不变量 1:运行时零 LLM token(★ 最关键)

**承诺:** Recipe 复用时,从 Pi Agent 调 `nurture_execute` 到操作完成,**仅一次 LLM round-trip**(那次工具调用本身)。所有 step 级别的导航、点击、验证全部走确定性 Python。

**保证机制:**

```
LLM/no-LLM 边界:nurture_execute 工具的 HTTP 调用
       │
       ▼
Pi Agent (LLM 区) ──HTTP──▶ Python Device Service (no-LLM 区)
                              │
                              ▼
                          AdaptiveStep:
                            Tier 0: detect_page (XML 解析,0 token)
                            Tier 1: load main.py + exec (0 token)
                            Tier 2: VLM fallback (★ 仅当无 recipe)
```

**测试基线:**
- 集成测试:某高频 operation 第二次执行时 LLM call 数 == 1
- import-graph 检查:`plugins/nurture/python/openclaw_agent/` 不允许 import `hermes.skills` / `hermes.cli` / `hermes.run_agent`
- 性能基线:warm path 端到端延迟与 OpenClaw 现有实现差异 < 5%

**违反样例(代码 review 必须拒):**
- AdaptiveStep 里调 `auxiliary_client.create_completion(...)` 做"智能选择"
- Pi Agent 里直接调 step 级工具(应该只调 operation 级)
- recipe 代码内嵌 LLM 调用做参数推理

---

## 不变量 2:Recipe 的权威源是 Python file system

**承诺:** Recipe 文件读写权全部在 Python 设备服务手里。Hermes 侧(TS plugin 入口)永远不直接写 Recipe。

**目录结构(权威):**

```
~/.hermes/nurture/data/recipes/<app>/<operation>/<step>/
├── main.py                # active recipe
├── main.meta.json         # 指标 + 版本元数据(schema 与 OpenClaw 1:1)
├── main.candidate.py      # reviewer 候选版本
├── main.candidate.meta.json
├── main.previous.py       # rollback 目标
└── main.previous.meta.json
```

**写者白名单:**
- `RecipeGenerator` (从 trace 生成)
- `RecipeReviewer` (复盘)
- `maybe_adopt_community_recipe` (社区同步)
- `rollback_to_previous` (自动回退)
- `hermes nurture migrate`(一次性迁移)

**禁止:**
- Hermes plugin 入口(plugins/nurture/__init__.py)直接写 recipes/
- skill_adapter / SKILL.md(因 ADR-005 不再实现)
- Curator(因 ADR-005 不接触)

---

## 不变量 3:Persona/Goals 注入只在 session 起始

**承诺:** Persona/Goals 通过 `pre_llm_call` hook 注入 system prompt。**Hermes prompt caching 政策禁止 mid-conversation 修改 system prompt**,因此:

- 注入时机:session start 后第一次 LLM call
- 文件被外部修改后,需要 `/new` 或 `/compress` 才生效
- 用户文档必须明确说明此约束

**实施约束:**
- pre_llm_call hook 必须做幂等检查 — 同 session 内只注入一次
- 注入内容用 `<nurture-persona>` / `<nurture-goals>` / `<nurture-capabilities>` XML 标签包裹,便于 LLM 识别和后续 compression 保留

---

## 不变量 4:社区下行 = 拉,不是推

**承诺:** 设备从社区拉数据,社区从不推(ADR-001)。

**禁止:**
- 设备插件起独立 HTTP listener 接收推送
- 设备 cron 直接订阅社区 webhook

**允许:**
- 设备 background thread 周期 GET 社区 endpoint
- 社区端用 polling token + ETag 优化带宽

---

## 不变量 5:Self-scoped 安全

**承诺:** 设备只能拉/操作"自己的"数据(以 avatarId 为边界)。

**实施:**
- 社区 FastAPI 所有 self-scoped 端点要求 Bearer token,token bind avatarId
- 服务端从 token 解出 avatarId,与请求 query 中的 avatarId 校验一致;不一致直接 403
- 任务/建议/指令 polling 必须按 token 解出的 avatarId 过滤,**绝不接受请求方传的 avatarId**
- `avatars.list` / `nurture.task.create` / `nurture.directive.create` 等管理操作要求 admin token

---

## 不变量 6:跨进程同步用 sentinel 文件

**承诺:** Python 设备服务与 Hermes plugin 之间不互相 RPC 写状态(避免循环依赖)。任何需要跨进程同步的状态变化,通过 sentinel 文件 + watchdog 完成。

**典型场景:**
- 社区 recipe cache 更新 → Hermes plugin 写 `~/.hermes/nurture/agent/community-recipes-cache.json`
- Python 服务下次 `RecipeStore.get()` 检查 mtime 决定是否消费
- Python 服务不直接 RPC 通知 Hermes

**反向同样:**
- Python 服务发现新 trace → 写 traces/<app>/<op>.jsonl
- Hermes plugin 的 background thread 60s 后通过 HTTP `/api/traces/pending` 拉取上报

---

## 不变量 7:配置不可变(per-session)

**承诺:** Hermes plugin 配置(`config.yaml` 里的 `plugins.nurture.*`)在 session 起始时读一次,session 内不重读。

**理由:**
- 与 prompt caching 兼容
- 避免动态配置与运行时状态不一致

**例外:**
- 仅 hermes 重启或新 session 才读取最新配置
- 配置变更需要用户显式触发 `/new` 才生效

---

## 不变量 8:多 agent 隔离 = profile 级别

**承诺:** 多设备/多 agent 运行时,每个 hermes 实例对应一个 hermes profile,profile 之间数据完全隔离(state / sessions / config / nurture data 各自独立)。

**实施:**
- `hermes profile create agent-deviceA`
- `hermes --profile agent-deviceA nurture init`
- `hermes --profile agent-deviceA gateway start`
- 多个 profile 共享同一个社区 Hermes 实例(社区也是一个独立 profile,例如 `--profile community`)

---

## 违反不变量的处理

PR review checklist 必须包含本文档每条不变量的检查项。如发现违反:

1. 暂停 PR
2. 在 PR 评论中引用具体不变量编号
3. 要求修改或在 boundary-contracts.md 提议变更不变量(需 owner 批准)
4. 不允许通过 monkey-patch 或绕路实现绕过不变量

---

## 测试矩阵

每条不变量对应至少一条自动化测试:

| 不变量 | 测试位置 |
|---|---|
| 1 — 零 LLM token | `tests/nurture/test_zero_llm_warm_path.py` (集成) |
| 2 — Recipe 权威源 | `tests/nurture/test_recipe_writer_isolation.py` (单测 + import-graph) |
| 3 — Persona 注入时机 | `tests/nurture/test_persona_injection_timing.py` |
| 4 — 社区拉模型 | `tests/nurture-community/test_no_push_endpoints.py` (端点列表审计) |
| 5 — Self-scoped | `tests/nurture-community/test_self_scoped_isolation.py` |
| 6 — Sentinel 同步 | `tests/nurture/test_recipe_cache_sync.py` |
| 7 — 配置不可变 | `tests/nurture/test_config_immutable_in_session.py` |
| 8 — Profile 隔离 | `tests/nurture/test_profile_isolation.py` |
