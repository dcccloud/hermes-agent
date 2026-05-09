"""pre_llm_call hook — inject capabilities + persona + goals into agent context.

Hermes prompt-caching policy: context is injected into the **user message**
of each turn (not the system prompt) to preserve the cache prefix.

Replaces OpenClaw's ``before_prompt_build`` hook (which had separate
``appendSystemContext`` / ``prependContext`` slots). See
``docs/avatar-hermes/boundary-contracts.md`` 不变量 3.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .device_bridge import DeviceBridge
from .device_tools import CachedCapabilityProvider, CachedPersonaProvider

logger = logging.getLogger(__name__)


_STATIC_GUIDANCE = """## 重要规则:设备操作必须使用 nurture_execute 工具

你是一个设备自动化 agent。你通过调用工具来控制手机,**绝对不能**自己模拟或想象操作结果。

### 核心规则(必须严格遵守)

1. **所有设备操作必须且只能通过 `nurture_execute` 工具执行**
   - 点赞、滑动、打开APP、关注等一切设备操作 → 调用 `nurture_execute`
   - **禁止**自己描述操作过程(如"我点击了屏幕")而不调用工具
   - **禁止**使用 image 工具读取截图 — 截图由设备服务内部处理
   - **禁止**使用 exec/shell 执行 adb 命令 — 一切通过 `nurture_execute`

2. **nurture_execute 调用格式**
   - `operation`: 从 <nurture-capabilities> 中选择,如 `douyin.give_a_like`
   - `params`: 可选参数,大多数操作用默认值即可,不要添加多余参数
   - 示例: `{"operation": "douyin.give_a_like"}`
   - 示例: `{"operation": "douyin.swipe_to_next_video", "params": {"count": 3}}`

3. **调 `<app>.do`(VLM 通用入口)时,task_prompt 必须显式带上 app 名字**
   - ❌ 错: `{"operation": "facebook.do", "params": {"task_prompt": "向下滑到下一个视频并点赞"}}`
   - ✅ 对: `{"operation": "facebook.do", "params": {"task_prompt": "在 Facebook 主页 feed 里向下滑到下一个视频,然后给视频点赞"}}`

### 如何选择操作

- 查看 <nurture-capabilities> 列出的可用操作
- has_recipe=true 的操作可靠(确定性 RPA)
- has_recipe=false 的操作使用 VLM 回退(较慢)

## 社区集成(同步查询)

通过 MCP 注册的远端工具可以查询社区(**仅限查询,不是设备操作**):
- `nurture.task.query` / `nurture.recipes.get` / `nurture.graph.get` / `nurture.advice.query` / `nurture.directive.query`

**任务执行流程(社区下发的任务,消息以 `[Community Task <taskId>]` 开头)**
1. 收到任务 → 阅读 <nurture-persona> 和 <nurture-goals>
2. 判断任务是否与人设一致(冲突则**直接走第 6 步上报 outcome=refused**,不执行)
3. 根据人设偏好 + <nurture-capabilities> 规划具体操作步骤
4. 逐步调用 `nurture_execute` 执行每个操作
5. 执行记录会自动上报社区
6. **结束时必须调用 `nurture_task_report` 工具**,传入:
   - `taskId`(消息开头的那个 id)
   - `outcome`:`success` / `failed` / `refused` / `partial`
   - `reason`(简短原因)
   - `summary`(可选,做了什么)

## 人设系统

你代表主人在社交媒体平台上活动。<nurture-persona> 是主人在该平台的人设档案,<nurture-goals> 是你的总体目标。

### 人设引导规则

- **必接任务**:与人设高度一致 + 有助于核心目标
- **可接任务**:与人设无冲突 + 有任务奖励
- **拒绝任务**:与人设明显冲突(如美食博主去点赞体育内容)
- 执行互动操作时(点赞、评论、关注),优先选择符合人设偏好的内容
- 如果某平台"尚未建立人设档案",建议先依次执行 view_my_profile、scrape_overview、scrape_fan_analysis、scrape_content_analysis 采集数据后建立人设
"""


def _format_capabilities(caps: Dict[str, Any]) -> str:
    """Format the capabilities response as a markdown block for the LLM."""
    lines: List[str] = []
    apps = caps.get("apps") or {}
    for app_name, app_data in apps.items():
        if not isinstance(app_data, dict):
            continue
        lines.append(f"### {app_name}")
        operations = app_data.get("operations") or []
        for op in operations:
            if not isinstance(op, dict):
                continue
            steps = op.get("steps") or []
            if steps:
                rates = [s.get("success_rate", 0) for s in steps if isinstance(s, dict)]
                rate = round(sum(rates) / len(rates)) if rates else 0
            else:
                rate = 0
            recipe_tag = "recipe" if op.get("has_recipe") else "vlm-fallback"
            desc = f" — {op['description']}" if op.get("description") else ""
            display_name = op.get("display_name") or op.get("name", "")
            lines.append(
                f"- **{op.get('id', '?')}** ({display_name}) "
                f"[{recipe_tag}, {rate}% success]{desc}"
            )
            for p in op.get("parameters") or []:
                if not isinstance(p, dict):
                    continue
                req = "required" if p.get("required") else "optional"
                lines.append(
                    f"  - `{p.get('name')}` ({p.get('type')}, {req}): {p.get('description')}"
                )
        graph_summary = app_data.get("graph_summary") or {}
        if isinstance(graph_summary, dict):
            page_count = graph_summary.get("total_pages", "?")
            lines.append(f"- App graph: {page_count} known pages")
    return "\n".join(lines)


def make_pre_llm_call_hook(
    cap_provider: CachedCapabilityProvider,
    persona_provider: CachedPersonaProvider,
) -> Callable[..., Optional[Dict[str, Any]]]:
    """Build the pre_llm_call callback.

    Returns ``{"context": "..."}`` per Hermes contract — the string is
    appended to the current turn's user message (cache-friendly, see
    ``hermes_cli/plugins.py`` invoke_hook docstring).

    Returns None when there are no capabilities yet (Python service still
    starting). The agent still works — just without the rich context.
    """

    def hook(**_kwargs: Any) -> Optional[Dict[str, Any]]:
        t0 = time.monotonic()
        caps = cap_provider.get()
        if not caps or not (caps.get("apps") or {}):
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.debug("nurture: pre_llm_call no caps yet (elapsed=%dms)", elapsed)
            return {"context": (
                "<nurture-capabilities>\n"
                "Device service is starting or no capabilities available yet. "
                "Wait a moment and try again.\n"
                "</nurture-capabilities>\n\n"
                + _STATIC_GUIDANCE
            )}

        apps = list((caps.get("apps") or {}).keys())
        formatted = _format_capabilities(caps)
        persona_data = persona_provider.get(apps)
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.info(
            "nurture: injecting %d apps into agent context (elapsed=%dms)",
            len(apps), elapsed,
        )

        parts: List[str] = []
        goals = persona_data.get("goals") or ""
        if goals:
            parts.append(f"<nurture-goals>\n{goals}\n</nurture-goals>")

        personas: Dict[str, Dict[str, Any]] = persona_data.get("personas") or {}
        for app in apps:
            p = personas.get(app)
            if p and p.get("exists") and p.get("raw"):
                parts.append(
                    f'<nurture-persona platform="{app}">\n{p["raw"]}\n</nurture-persona>'
                )
            else:
                parts.append(
                    f'<nurture-persona platform="{app}">\n'
                    f"[{app}] 尚未建立人设档案。建议先执行 {app}.view_my_profile、"
                    f"{app}.scrape_overview、{app}.scrape_fan_analysis、"
                    f"{app}.scrape_content_analysis 采集数据后建立人设。\n"
                    f"</nurture-persona>"
                )

        parts.append(f"<nurture-capabilities>\n{formatted}\n</nurture-capabilities>")
        parts.append(_STATIC_GUIDANCE)

        return {"context": "\n\n".join(parts)}

    return hook
