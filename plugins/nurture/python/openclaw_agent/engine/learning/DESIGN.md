# Self-Evolving RPA Engine - Design Document

## 1. Overview

OperationEngine 的核心执行框架。目标是让客户端的 RPA 自动化能力**随着使用不断进化**——执行效率越来越高、token 消耗越来越低、对 APP 的理解越来越深。

系统由三个自进化组件组成，它们相互协作：

```
┌─────────────────────────────────────────────────────────┐
│                   Self-Evolving RPA Engine                │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │  APP State    │  │  Adaptive    │  │   Recipe     │   │
│  │  Graph        │  │  Step        │  │   Generator  │   │
│  │              │  │              │  │              │   │
│  │  理解 APP 的  │  │  执行并      │  │  从经验中    │   │
│  │  页面结构     │  │  学习        │  │  生成代码    │   │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘   │
│         │                 │                  │           │
│         └────────┬────────┴──────────┬───────┘           │
│                  ▼                   ▼                   │
│         ┌──────────────┐   ┌──────────────────┐         │
│         │  TraceStore   │   │  RecipeStore     │         │
│         │  (VLM 经验)   │   │  (学习到的代码)   │         │
│         └──────────────┘   └──────────────────┘         │
└─────────────────────────────────────────────────────────┘
```

### 进化循环

1. **执行** — AdaptiveStep 用 Recipe/RPA 执行操作
2. **兜底** — 失败时 VLM 接管，完成任务
3. **记录** — VLM 的操作轨迹保存到 TraceStore
4. **发现** — 到达新页面时，通过 Vision LLM 识别并注册到 APP State Graph
5. **学习** — RecipeGenerator 异步消费 trace，生成新的 Python recipe
6. **替代** — 下次执行时，learned recipe 替代 VLM，更快更省

---

## 2. APP State Graph

### 2.1 核心概念

APP State Graph 是对一个 APP 所有已知页面状态和页面间导航关系的图模型。
它是整个系统的"地图"——recipe 靠它知道自己在哪、要去哪、怎么去。

#### 2.1.1 初始状态（Initial State）

每个 APP 有且只有一个**初始状态**：杀掉进程后重新启动到达的那个页面。

- 抖音的初始状态：`app_home`（推荐 Feed 流页面）
- 初始状态是整个状态图的**根节点**，所有其他状态的 `discovery_path` 都从它开始
- 初始状态在 seed 图中硬编码，不可被覆盖

初始状态的意义：**锚定**。当系统迷路时（不知道在哪个页面），可以杀掉 APP
重新启动，回到确定性的初始状态，然后从已知位置重新导航。

#### 2.1.2 状态的定义

**状态 = UI 布局结构，而非内容。**

| 场景                              | 是否新状态 | 原因                      |
| --------------------------------- | ---------- | ------------------------- |
| Feed 流刷到视频 A → 视频 B        | 否         | UI 布局相同，只是内容不同 |
| Feed 流刷到视频 → 刷到直播间      | 是         | UI 布局完全不同           |
| 数据中心页面向下滚动              | 否         | 同一页面的不同位置        |
| 个人主页 → 他人主页               | 是         | 功能区域和按钮不同        |
| 数据中心"总览"tab → "作品分析"tab | 是         | 内容区域完全不同          |

#### 2.1.3 Optional 状态

某些状态是**可能出现也可能不出现**的，比如：

- 弹窗提示（"是否允许通知"）
- 广告插屏
- 版本更新提示
- 登录确认框

这些状态标记为 `is_optional: true`。Recipe 在导航时需要处理它们
（如果出现就关掉，如果没出现就跳过），但它们不是稳定的页面状态。

### 2.2 数据模型

```python
class AppState:
    state_id: str              # 唯一标识符，如 "data_center"
    name: str                  # 人类可读的名称，如 "数据中心 - 总览"
    description: str           # 功能描述，如 "展示账号诊断和经营数据的页面"
    discovery_path: list[str]  # 从初始状态到达的路径
                               # 如 ["app_home", "profile", "creator_center", "data_center"]
    indicators: list[dict]     # 用于 detect_page() 的文本指标
                               # [{"text": "总览", "type": "textContains", "source": "seed"}]
    transitions: dict          # 已知的出边（可以去哪些状态）
                               # {"creator_center": {"hint": "按返回键", "seen_count": 3}}
    is_optional: bool          # 是否为可选状态（弹窗/广告等）
    seen_count: int            # 被观测到的次数
    source: str                # "seed"（手动定义）或 "discovered"（自动发现）
```

### 2.3 图的分层架构

```
Seed Graph (代码硬编码)  ──┐
                           ├──► Merged Runtime Graph
Learned Graph (JSON 持久化) ─┘
```

- **Seed Graph**：开发者手动定义的基线状态图。包含最核心的已知页面。
  类似于 recipe 系统中的 seed RPA（v0）。
  文件：`learning/app_pages/douyin.seed.json`（原 `douyin.py` 仅保留为兼容入口）

- **Learned Graph**：系统运行过程中自动发现的新状态和转移关系。
  持久化为 JSON 文件。
  文件：`data/app_graphs/douyin.json`

- **Merged Graph**：运行时合并 seed + learned，learned 优先。
  这是 `detect_page()`、`find_path()`、recipe prompt 使用的实际图。

### 2.4 状态发现算法

当 AdaptiveStep 通过 VLM 兜底成功到达一个页面后，触发以下流程判断
是否发现了新状态：

```
VLM 兜底成功，到达了某个页面
        │
        ▼
  ┌─────────────────────────┐
  │ 是否通过已知 transition  │
  │ edge 到达？              │
  └────────┬────────────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
    是           否（VLM 探索到达）
     │            │
     ▼            ▼
  已知状态    ┌─────────────────┐
  record_    │ detect_page()    │
  visit      │ 匹配全图        │
  结束        └────────┬────────┘
                       │
                 ┌─────┴─────┐
                 ▼           ▼
              匹配到 X    没匹配到
                 │            │
                 ▼            ▼
           不是新状态    ┌─────────────────┐
           记录新的     │ 稳定性检查       │
           转移边       │ (等 1-2 秒再截屏) │
           prev→X      └────────┬────────┘
                                │
                          ┌─────┴─────┐
                          ▼           ▼
                      页面消失/    页面稳定
                      变化          │
                          │         ▼
                          ▼    ┌──────────────────┐
                     标记为    │ vision_read LLM   │
                     optional  │ 识别新状态         │
                     状态      │                    │
                               │ 输入：             │
                               │ - 截图             │
                               │ - 已知全图          │
                               │ - 到达路径          │
                               │                    │
                               │ 输出：             │
                               │ - state_id         │
                               │ - name             │
                               │ - description      │
                               │ - indicators       │
                               │ - is_optional       │
                               └────────┬───────────┘
                                        │
                                        ▼
                                 注册新状态到图
                                 记录转移边
                                 持久化到 JSON
```

#### 关键设计决策

**为什么不做精确的 UI 比对去重？**

两个页面是否"相同"本质上是模糊判断。与其花费大量精力做不完美的比对
（文本指标可能匹配错误、截图像素比对受内容影响），不如：

- 让 vision_read LLM 看截图，由它判断"这是新页面还是已知页面"
- 如果两个实际相同的页面被创建为两个状态（类似的名字和 indicators），
  在功能上也不影响——recipe 都能用
- 偏向保守（宁可多创建状态，不遗漏）

**为什么只有 VLM 探索才触发发现？**

Recipe 是从 VLM trace 生成的，所以 recipe 点击的按钮都是 VLM 之前
已经点过的——不会产生真正的"新按钮点击"。只有 VLM 在兜底时
可能探索到未知区域。唯一例外是 APP 更新了 UI，但这种情况下
verify_fn 会失败 → VLM 接管 → 依然回到 VLM 探索的流程。

### 2.5 改进的页面发现机制

原始的状态发现流程（2.4）依赖 `detect_page()` 进行文本指标匹配，
在探索性操作（`target_page=None`）中存在严重缺陷：

- 评论浮层的 UI hierarchy 仍包含底层 video_player 的文本 → 误认为 video_player
- 商城页面的 hierarchy 包含通用的 tab 栏文本 → 误认为 app_home
- **根本原因**：`detect_page` 依赖全文搜索 dump_hierarchy，无法区分叠加层

#### 改进策略：三层信号

```
VLM 任务成功
    │
    ├── target_page 有值?
    │   │
    │   ├── YES → 快速路径（原有逻辑）
    │   │         detect_page 文本匹配 + 记录 hash
    │   │
    │   └── NO  → 探索路径（新逻辑）
    │             │
    │             ▼
    │       计算截图 perceptual hash
    │             │
    │       ┌─────┴─────┐
    │       ▼           ▼
    │   Hamming < 12  Hamming >= 12
    │       │           │
    │       ▼           ▼
    │   indicator     调 VLM discover_state
    │   二次确认       传入 vlm_finish_message
    │       │          作为上下文
    │   ┌───┴───┐        │
    │   ▼       ▼        │
    │  通过    不通过     │
    │   │       │        │
    │   ▼       └───►────┤
    │  已知页面           │
    │  记录 visit   ┌────┴─────┐
    │  更新 hash    ▼          ▼
    │            is_new=true  is_new=false
    │              │           │
    │              ▼           ▼
    │            注册新页面   匹配已知页面
    │            存 indicator 更新 hash 集合
    │            存 hash
    │
    └────── 不调 detect_page ──────
```

#### 关键改动

1. **VLM 上下文传递**：`_save_trace()` 从 VLM 的最后一个 `finish` action
   中提取完成信息（如 "我已进入商城"），通过 `_update_graph()` →
   `_discover_new_state()` → `discover_state()` 传递给页面发现 prompt。
   这让 VLM 在判断页面身份时有更多上下文，而不仅靠截图猜。

2. **探索模式 bypass**：当 `target_page is None`，完全跳过 `detect_page()`
   文本匹配。先查 perceptual hash 库，如果 Hamming 距离 < 12（视觉高度相似），
   需通过 indicator 二次确认后才按已知页面处理。否则调 VLM `discover_state`。

3. **Hash 命中后 indicator 二次确认**（`_verify_hash_by_indicators`）：
   hash 匹配只表示视觉相似，可能产生假阳性（如相机页与评论页都有大面积深色区域）。
   确认流程：取匹配页面的 `indicators` 列表，逐个在当前屏幕 hierarchy 中检查
   `textContains` 是否存在；命中数 >= `match_threshold`（默认 2）视为确认通过。
   无 indicators 的页面直接信任 hash。

4. **Tap-to-pause 稳定性重试**：`_discover_new_state` 中若 `check_stability`
   首次失败（如视频流页面像素变化 > 50%），会尝试点击屏幕中心（暂停视频播放），
   等待 0.5s 后以 `wait_seconds=1.0` 重试稳定性检查。仍不稳定才标记为 transient
   跳过发现。这避免了视频 feed 等动态页面被误判为不稳定而丢失发现机会。

5. **Perceptual Hash 视觉指纹**（`screenshot_hash.py`）：
   - 截图 → 裁掉状态栏（顶部 5%）→ resize 到 8×8 灰度 → 平均阈值 → 64-bit hash
   - 存储为 16 字符 hex 字符串
   - 每个页面最多存 5 个参考 hash（在 `AppState.screenshot_hashes` 字段）
   - `find_closest_page_by_hash()` 遍历所有已知页面的 hash，返回最近匹配

6. **Hash 积累时机**：
   - `discover_state` 注册新页面时，存储第一个 hash
   - `discover_state` 匹配到已知页面时，追加 hash
   - `detect_page` 匹配成功时（有 target_page 的快速路径），追加 hash
   - 探索模式 hash 命中且 indicator 确认通过时，追加 hash

### 2.6 状态图的用途

1. **AdaptiveStep 预检**：如果 `target_page` 已设置且 `detect_page()` 返回
   目标页面，跳过整个步骤（零成本）。

2. **Recipe 生成 prompt**：注入完整状态图，让 LLM 生成的 recipe 代码
   知道怎么检测当前页面、怎么导航到目标。

3. **Recipe 运行时**：recipe 代码可以直接 `import` 并调用
   `detect_page(device)` 和 `find_path(start, goal)`。

4. **导航规划**：`find_path()` 用 BFS 找两个页面之间的最短路径，
   帮助 recipe 从任意已知页面导航到目标。

5. **人类调试**：通过 `/api/graph/{app}` 端点查看当前状态图，
   理解系统对 APP 的认知程度。

---

## 3. AdaptiveStep (Two-Tier Execution)

### 3.1 执行模型

每个 operation 由一个或多个 AdaptiveStep 组成。每个 step 是一个
"从当前状态到目标状态"的转换。

```python
step = AdaptiveStep(
    operation="douyin/enter_data_center",
    step="go_to_data_center",
    target_page="data_center",       # 目标页面（用于预检和图更新）
)
ok = step.run(
    device, agent,
    rpa_fn=lambda: self._try_click(device),  # seed RPA（v0 recipe）
    vlm_prompt="点击数据中心入口...",           # VLM 兜底指令
    verify_fn=lambda: self._verify(device),  # 执行后验证
    nav_path=nav_path,                        # 导航路径追踪
)
```

### 3.2 执行流程

```
┌─────────────────────────────────────────┐
│  Tier 0: Pre-check                      │
│  detect_page() == target_page?          │
│  是 → return True (跳过)                │
└────────────────────┬────────────────────┘
                     │ 否
                     ▼
┌─────────────────────────────────────────┐
│  Tier 1: Recipe                         │
│  有 learned recipe → 用 learned recipe  │
│  没有 → 用 seed rpa_fn (v0)            │
│  执行后用 verify_fn 验证                │
│  成功 → return True                     │
└────────────────────┬────────────────────┘
                     │ 失败
                     ▼
┌─────────────────────────────────────────┐
│  Tier 2: VLM Fallback                   │
│  agent.run(vlm_prompt)                  │
│  执行后用 verify_fn 验证                │
│  成功 → save trace + 触发状态发现       │
│  失败 → return False                    │
└─────────────────────────────────────────┘
```

### 3.3 Trace 捕获

当 VLM 兜底成功时，保存两部分信息：

1. **rpa_commands_before_fallback**：recipe/rpa_fn 在失败前执行的 u2 命令
   （从 DeviceRecorder 获取，说明 recipe 做到了哪一步）
2. **VLM action trace**：VLM 的每步操作（坐标、action_type、thinking）
   （从 AgentRecorder 获取，说明 VLM 怎么补完的）

这两部分一起给到 RecipeGenerator，让它生成覆盖完整流程的新 recipe。

### 3.4 导航路径追踪

Operation 在执行过程中维护一个 `nav_path: list[str]`，记录经过的页面序列。
每个 AdaptiveStep 成功后，将 `target_page` 追加到 `nav_path`。

这个路径用于：

- 状态发现时告诉 LLM "我是怎么到达这个页面的"
- 作为新状态的 `discovery_path`

---

## 4. Recipe System

### 4.1 Recipe 是什么

Recipe 是 LLM 从 VLM 操作轨迹中学习生成的 **Python RPA 代码**。
它存储为 `.py` 文件，可以被动态加载并执行。

每个 recipe `.py` 文件包含两个函数：

```python
# recipe_meta: target_page=data_center, verify_type=page, description=导航到数据中心

from openclaw_agent.engine.learning.humanize import detect_page, tap_region, wait

def execute(device) -> bool:
    """执行操作，到达目标状态。"""
    # ... 导航/操作逻辑 ...
    return True

def verify(device) -> bool:
    """独立验证目标是否达成（只检查，不操作）。"""
    return detect_page(device) == "data_center"
```

- `execute(device) -> bool`：执行操作的主逻辑
- `verify(device) -> bool`（可选）：独立的验证函数，由 AdaptiveStep 在
  execute 返回后调用。与 execute 分离，使验证逻辑可以利用三层页面识别
  机制（text indicator + perceptual hash + VLM），而不是靠 execute 内部
  的 `textContains` 匹配

```
data/recipes/
  douyin/
    enter_data_center/
      go_to_me_tab.py          # 从任意页面导航到"我"tab
      go_to_me_tab.meta.json   # 成功/失败统计 + 元数据
      go_to_creator_center.py
      go_to_data_center.py
    give_a_like/
      tap_like.py
```

### 4.2 Recipe 的生成

RecipeGenerator 异步消费 TraceStore 中的 pending traces，
调用 LLM（vision_read model）生成 Python 代码。

**生成 prompt 包含**：

- VLM 操作轨迹（actions + rpa_commands_before_fallback）
- Humanize API 文档（tap_region、swipe_region、wait、back 等）
- **APP 状态图**（完整的页面和转移关系）
- 状态机思维要求（先 detect_page，已在目标则跳过）

**生成代码要求**：

- 函数签名：`def execute(device) -> bool`
- 必须同时生成 `def verify(device) -> bool` 独立验证函数
- 必须包含 `# recipe_meta:` 元数据注释（target_page、verify_type、description）
- 必须使用 humanize API（不直接调 device.click）
- 优先用 u2 元素选择器，坐标区域作为后备
- 函数开头必须 `detect_page()` 检测当前页面
- 已在目标页面直接 `return True`
- 关键操作后有验证
- 只允许 import `openclaw_agent.engine.learning.humanize`

### 4.3 Recipe 的生命周期

```
1. seed RPA (v0)     → 手写的原始 u2 代码，作为初始 recipe
2. VLM 兜底成功      → trace 保存到 TraceStore
3. RecipeGenerator   → 消费 trace，生成 learned recipe (v1)
4. learned recipe    → 替代 seed RPA，下次优先使用
5. learned recipe 失败 → VLM 再次兜底 → 新 trace
6. RecipeGenerator   → 从多条 trace（含 v1 的失败命令 + VLM 的补救）
                        生成改进版 recipe (v2)
7. ...不断迭代
```

### 4.4 Recipe 的监控与元数据

每个 recipe 的 `.meta.json` 记录运行指标和元数据：

**运行指标**（详见 §6.2）：

- `success` / `failure` / `total_runs` / `streak` / `vlm_fallback_count`
- `avg_duration_ms` / `last_duration_ms` / `last_run_ts`
- `disabled`：失败率过高时自动禁用

**元数据**（由 RecipeGenerator 从代码注释中提取）：

| 字段          | 类型        | 说明                                        |
| ------------- | ----------- | ------------------------------------------- |
| `target_page` | string/null | 预期目标页面 ID（用于三层验证）             |
| `verify_type` | string/null | `"page"` / `"custom"` / `"vlm"`             |
| `description` | string      | 操作描述                                    |
| `frozen`      | bool        | 是否冻结（人类编写的 op 为 true，跳过进化） |

### 4.5 验证优先级

AdaptiveStep 在 recipe.execute() 成功后，按以下优先级选择验证函数：

1. **外部 verify_fn**（Operation 级别传入）— 最高优先级
2. **recipe.verify()**（recipe .py 文件中定义的独立验证函数）
3. **None**（信任 execute() 的返回值）

这个设计确保：

- 人类编写的 Operation 的 verify_fn 始终优先（向后兼容）
- LLM 生成的 recipe 可以自带验证逻辑
- 旧 recipe（没有 verify 函数）也能正常工作

### 4.6 Humanize（拟人化）

所有 recipe 必须使用 `openclaw_agent.engine.learning.humanize` 模块中的
拟人化操作原语，而不是直接调用 u2 API：

| humanize API                       | 作用             | 拟人化特征              |
| ---------------------------------- | ---------------- | ----------------------- |
| `tap_region(device, region)`       | 在区域内随机点击 | 随机坐标 + 随机按压时长 |
| `swipe_region(device, start, end)` | 区域到区域的滑动 | Bezier 曲线轨迹         |
| `wait(base, spread)`               | 等待             | 三角分布随机时长        |
| `back(device)`                     | 按返回键         | 固定行为 + 等待         |
| `detect_page(device, app)`         | 检测当前页面     | -                       |
| `find_path(start, goal, app)`      | 查找导航路径     | -                       |

---

## 5. GenericVlmTask（万能 VLM 操作）

对于完全没有 seed RPA 的全新任务，`GenericVlmTaskOperation` 提供了
"从零开始学习"的能力：

```python
# 完全靠 VLM 执行，rpa_fn=lambda: False 确保直接走 VLM
step = AdaptiveStep(f"{app}/{task_name}", "main")
step.run(device, agent,
    rpa_fn=lambda: False,   # 没有 seed，直接 VLM
    vlm_prompt=task_prompt,
)
```

首次执行：纯 VLM 完成任务 → trace 保存
后续执行：RecipeGenerator 从 trace 生成 recipe → 逐步替代 VLM

这使系统能够学习**任意未预定义的操作**。

---

## 6. 质量迭代循环（LLM Review + Candidate Alternation）

### 6.1 概述

替代原来「VLM 兜底就触发 recipe 生成」的被动策略，改为**基于运行指标的 LLM 主动复盘**。
每次 recipe 执行后，异步检查指标是否需要优化；需要时 LLM 生成候选 recipe，
通过轮流执行对比来验证改进效果。

### 6.2 指标体系

`.meta.json` 记录以下扩展指标：

| 字段                 | 类型  | 说明                     |
| -------------------- | ----- | ------------------------ |
| `success`            | int   | 累计成功次数             |
| `failure`            | int   | 累计失败次数             |
| `total_runs`         | int   | 总执行次数               |
| `streak`             | int   | 连续成功次数（失败归零） |
| `last_run_ts`        | float | 最近一次执行时间戳       |
| `last_duration_ms`   | float | 最近一次耗时（ms）       |
| `avg_duration_ms`    | float | 滚动平均耗时（ms）       |
| `vlm_fallback_count` | int   | VLM 兜底介入次数         |
| `disabled`           | bool  | 是否被自动禁用           |

### 6.3 优化触发

每次执行后，`_post_run_async()` 异步执行：

1. **评估候选**：如果有 candidate 且已跑满 `_CANDIDATE_EVAL_RUNS` 次，比较指标决定晋升/回退
2. **LLM 复盘**：`RecipeReviewer.should_review()` 检查是否需要优化（前置条件）：
   - 总执行次数 >= 3
   - 不存在待评估的 candidate
   - 成功率 < 90% 或 VLM 兜底率 > 10%
3. 满足条件时，LLM 分析指标和代码，给出 `verdict: "good" | "needs_improvement"`
4. 需要优化时，自动生成 candidate recipe

### 6.4 Candidate 轮流评估

```
执行请求到达
    │
    ├─ 有 candidate？
    │   ├─ YES → 谁的 total_runs 少就用谁（保持平衡）
    │   │        current(n=3) vs candidate(n=2) → 用 candidate
    │   └─ NO  → 用 current（正常流程）
    │
    ▼
执行 + 记录指标（variant = "current" 或 "candidate"）
    │
    ▼
_post_run_async（异步）
    │
    ├─ candidate runs >= 3？ → 比较 → promote / rollback
    └─ should_review？ → LLM 复盘 → 可能生成新 candidate
```

### 6.5 晋升/回退判断

当 candidate 累计执行 >= `_CANDIDATE_EVAL_RUNS`（默认 3）次后：

- **晋升条件**：candidate 成功率 >= current 且 VLM 兜底率 <= current
- **回退条件**：不满足以上条件
- 晋升时：candidate.py → current.py，旧 current 备份为 .prev.py
- 回退时：删除 candidate 文件，保留回退原因日志

### 6.6 文件布局

```
data/recipes/douyin/enter_data_center/
├── go_to_data_center.py              # current recipe
├── go_to_data_center.meta.json       # current 指标
├── go_to_data_center.candidate.py    # candidate（优化版）
├── go_to_data_center.candidate.meta.json  # candidate 指标
├── go_to_data_center.prev.py         # 上一版 backup
└── go_to_data_center.prev.meta.json  # 上一版指标 backup
```

---

## 7. Operation 与 Recipe 的统一

### 7.1 概念模型

Operation（人类编写的操作）和 Recipe（LLM 生成的操作）本质上是同一个东西，
只有**成熟度**不同：

| 类型                 | 本质                       | 进化            | frozen |
| -------------------- | -------------------------- | --------------- | ------ |
| 人类编写的 Operation | 冻结的 recipe（v0 最优解） | 暂不进化        | true   |
| LLM 生成的 Recipe    | 可进化的 recipe            | candidate 轮替  | false  |
| 探索性任务           | 待学习的 recipe            | 初始生成 + 进化 | false  |

### 7.2 当前状态

人类编写的 Operation（如 `enter_data_center.py`）：

- 使用 `AdaptiveStep` 编排多个 step
- 每个 step 有内联的 `rpa_fn`（seed recipe）和 `verify_fn`
- 通过 `target_page` 参数指定目标页面

LLM 生成的 Recipe（如 `enter_shop/main.py`）：

- 独立的 `.py` 文件，包含 `execute()` 和 `verify()`
- 通过 `# recipe_meta:` 注释声明 `target_page` 等元数据
- 由 `AdaptiveStep` 动态加载并执行

### 7.3 统一验证机制

所有 recipe 的验证逻辑遵循统一设计：

**导航类 recipe**（`verify_type=page`）：

```python
def verify(device) -> bool:
    return detect_page(device) == "target_page_id"
```

`detect_page` 底层使用三层信号（text indicator + perceptual hash + VLM context），
避免了原来 `textContains` 匹配的歧义问题。

**操作类 recipe**（`verify_type=custom`）：

```python
def verify(device) -> bool:
    # 检查操作结果的具体 UI 证据
    return device(textContains="条评论").exists(timeout=3)
```

**探索类 recipe**（`verify_type=vlm`）：
由 VLM 判断或依赖 `_update_graph` 的页面发现机制。

### 7.4 未来迁移路径

让人类 Operation 也进入进化流程的步骤：

1. 把 Operation 的 `rpa_fn` 抽出为独立的 recipe `.py` 文件
2. 把 `verify_fn` 写入 recipe 文件的 `verify()` 函数
3. `.meta.json` 设 `frozen: true`
4. 取消 `frozen` 标记 → 自动进入 candidate 轮替进化

当前阶段不实现迁移，只确保 recipe 格式支持 `frozen` 字段，
为未来扩展预留接口。

---

## 8. 文件结构

```
openclaw_agent/engine/
├── learning/                    # 自进化核心
│   ├── DESIGN.md               # 本文档
│   ├── adaptive_step.py        # 两层执行框架 + 质量迭代
│   ├── recipe_store.py         # Recipe 存储和加载 + candidate 管理
│   ├── recipe_generator.py     # LLM 生成初始 recipe
│   ├── recipe_reviewer.py      # LLM 复盘 + candidate 生成
│   ├── operation_namer.py      # LLM 命名自动发现的操作
│   ├── trace_store.py          # VLM 操作轨迹存储（供 RecipeGenerator 消费）
│   ├── event_log.py            # 统一事件日志（全量活动记录 + 社区上报）
│   ├── humanize.py             # 拟人化操作原语
│   ├── app_graph.py            # 持久化 APP 状态图
│   ├── state_discovery.py      # Vision LLM 状态发现
│   ├── screenshot_hash.py     # 截图 perceptual hash 视觉指纹
│   ├── vision_utils.py         # Vision API 共享工具
│   └── app_pages/              # 各 APP 的 seed 状态图
│       ├── __init__.py
│       ├── douyin.seed.json    # 抖音 seed 图（JSON 格式）
│       └── douyin.py           # 兼容入口（仅 docstring）
├── data/                       # 运行时数据（git ignored）
│   ├── traces/                 # VLM 操作轨迹
│   │   └── douyin/
│   │       └── enter_data_center.jsonl
│   ├── recipes/                # 学习到的 recipe 代码
│   │   └── douyin/
│   │       └── enter_data_center/
│   │           ├── go_to_data_center.py
│   │           ├── go_to_data_center.meta.json
│   │           ├── go_to_data_center.candidate.py
│   │           └── go_to_data_center.candidate.meta.json
│   └── app_graphs/             # 学习到的状态图
│       └── douyin.json
└── operations/                 # 业务操作
    └── douyin/
        ├── enter_data_center.py  # 使用 AdaptiveStep 的操作
        └── ...
```

---

## 9. API Endpoints

| 端点                                    | 方法 | 用途                                     |
| --------------------------------------- | ---- | ---------------------------------------- |
| `/api/health`                           | GET  | 健康检查                                 |
| `/api/devices`                          | GET  | 列出已连接设备                           |
| `/api/execute`                          | POST | 执行单次操作                             |
| `/api/execute_task`                     | POST | 执行编排任务                             |
| `/api/capabilities`                     | GET  | 上报设备能力（recipe 列表 + graph 状态） |
| `/api/recipes`                          | GET  | 列出所有 learned recipes                 |
| `/api/recipes/{app}/{operation}/{step}` | GET  | 查看指定 recipe 的源代码                 |
| `/api/recipes/generate`                 | POST | 手动触发 recipe 生成                     |
| `/api/recipes/import`                   | POST | 从 Server 导入 recipe（知识分发）        |
| `/api/traces/pending`                   | GET  | 查看待处理的 VLM traces                  |
| `/api/graph/{app}`                      | GET  | 查看 APP 状态图                          |
| `/api/graph/{app}/merge`                | POST | 从 Server 合并共识状态图（知识分发）     |

---

## 10. Event Log（统一事件日志）

### 10.1 概述

EventLog 是一个统一的时间序列事件存储，记录设备端所有操作和见闻。
它与 TraceStore 互补——TraceStore 存储完整的 VLM action trace 供
RecipeGenerator 消费，EventLog 存储轻量事件元数据供社区上报、调试和分析。

### 10.2 存储

- 文件路径：`data/events/YYYY-MM-DD.jsonl`（每天一个文件）
- 14 天滚动保留，server 启动时自动清理
- Append-only 写入，线程安全（per-file lock）

### 10.3 事件记录格式

```json
{
  "id": "evt_a1b2c3d4e5f6",
  "event_type": "step.recipe_executed",
  "timestamp": "2026-04-05T08:12:33.456Z",
  "device_id": "emulator-5554",
  "task_id": "task-xxx",
  "operation": "douyin/enter_data_center",
  "step": "go_to_me_tab",
  "payload": { "variant": "current", "success": true, "duration_ms": 1234 }
}
```

信封字段（id, event_type, timestamp, device_id, task_id, operation, step）
顶层放置，便于高效过滤。`payload` 是事件类型特定的数据。

### 10.4 事件类型

| event_type                     | 触发时机                 | payload 要点                        |
| ------------------------------ | ------------------------ | ----------------------------------- |
| `operation.started`            | Executor 开始执行操作    | handler_class, op_index             |
| `operation.completed`          | Executor 操作结束        | status, duration_ms, vlm_used       |
| `step.started`                 | AdaptiveStep.run() 开始  | has_recipe, target_page             |
| `step.recipe_executed`         | recipe/seed 执行后       | variant, success, duration_ms       |
| `step.vlm_started`             | VLM fallback 开始        | prompt_preview                      |
| `step.vlm_completed`           | VLM fallback 结束        | success, actions_count, duration_ms |
| `step.completed`               | AdaptiveStep.run() 返回  | success, tier_used, duration_ms     |
| `page.discovered`              | VLM 发现新页面           | app, state_id, name, from_page      |
| `page.visited`                 | 到达已知页面             | app, state_id, via                  |
| `recipe.generated`             | RecipeGenerator 生成代码 | from_traces_count, source           |
| `recipe.candidate_promoted`    | candidate 晋升           | candidate_rate, current_rate        |
| `recipe.candidate_rolled_back` | candidate 回退           | reason                              |
| `recipe.community_adopted`     | 社区 recipe 被采纳       | global_success_rate, origin_node    |
| `recipe.disabled`              | 自动禁用                 | failure_rate, total_runs            |
| `precondition.failed`          | 前置条件检查失败         | reason                              |
| `precondition.recovered`       | 状态恢复后前置检查通过   | —                                   |

### 10.5 与 TraceStore 的关系

- **TraceStore 不动**：RecipeGenerator 依赖其 consumed/unconsumed 语义
- **EventLog 互补**：`_save_trace()` 写 TraceStore 的同时，也 emit `step.vlm_completed`
- 消费者需要完整 VLM trace → 查 TraceStore
- 消费者需要执行时间线 → 查 EventLog

### 10.6 社区上报

TS 侧 community-connector 每 30 秒通过 `GET /api/events?cursor=...` 拉取
新事件，通过 `nurture.event.report` 上报社区 Gateway。游标（cursor）
基于文件名+字节偏移，无需 consumed 标记，支持多消费者独立读取。

### 10.7 API

```
GET /api/events?since=<iso>&event_types=<comma>&cursor=<opaque>&limit=100
→ { "events": [...], "cursor": "...", "has_more": true }
```

---

## 11. 设计原则

1. **宁可多状态，不遗漏**：状态匹配不追求完美去重。两个实际相同的
   页面即使被创建为两个状态（类似的名字和 indicators），在功能上
   也不影响 recipe 的生成和执行。

2. **seed 是 v0，不是唯一**：无论是状态图还是 recipe，手写的 seed
   都只是初始版本。系统会通过运行逐步学习更好的版本。

3. **VLM 是探索者，recipe 是执行者**：VLM 负责探索未知区域并记录
   经验；recipe 负责将经验固化为高效、可复用的代码。

4. **异步学习，同步执行**：recipe 生成是后台异步任务（不阻塞操作
   执行），但 recipe 加载和执行是同步的（在操作过程中实时决策）。

5. **拟人化贯穿始终**：从 VLM 兜底（prompt 要求拟人操作）到
   recipe 代码（强制使用 humanize API），确保每次执行都有自然的
   随机性，避免被检测为自动化行为。

6. **状态机思维**：recipe 不是"盲目执行一串动作"，而是"检测当前
   在哪个页面 → 规划导航路径 → 执行 → 验证到达"的状态转换器。
