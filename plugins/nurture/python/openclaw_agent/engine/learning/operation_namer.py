"""LLM-powered naming for auto-discovered operations.

When NaturalLanguageOperation succeeds via VLM, this module decides
whether the task is worth learning as a reusable named operation and,
if so, derives a snake_case ``name`` and a human-readable
``display_name``.
"""

import json
from typing import Any

from openai import OpenAI

from openclaw_agent.engine.common.config import get_config
from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("operation_namer")

_SYSTEM_PROMPT = """\
你是 APP 操作命名专家。根据用户指令和 VLM 执行动作记录，判断这是否是一个值得保存为可复用操作的任务。

## 判断规则

1. **应该学习** — 通用、可复用的导航或操作（如"去消息页"、"打开设置"、"进入直播间"）
2. **不应该学习** — 一次性的、内容相关的任务（如"给张三发消息说你好"、"搜索关键词XXX"）
3. **去重** — 如果已有操作列表中已有功能相同的操作，返回 should_learn: false

## 命名规范

- name: 英文 snake_case，简洁（如 go_to_messages, open_settings）
- display_name: 简短中文名（如"去消息页面"、"打开设置"）

## 输出格式

返回 JSON（不要其他文字）：
```json
{"should_learn": true, "name": "snake_case_name", "display_name": "中文名"}
```
或
```json
{"should_learn": false, "reason": "原因"}
```"""


def name_operation(
    prompt: str,
    actions_summary: list[dict[str, Any]],
    app: str,
    existing_operations: list[str],
) -> dict[str, Any] | None:
    """Ask LLM whether this NL task should become a named operation.

    Returns a dict with ``should_learn``, ``name``, ``display_name``
    (when learning) or ``should_learn: false, reason`` (when not).
    Returns ``None`` on LLM / parse failure.
    """
    actions_brief = []
    for a in actions_summary[:8]:
        entry: dict[str, Any] = {
            "step": a.get("step"),
            "action_type": a.get("action_type"),
        }
        if a.get("thinking"):
            entry["thinking"] = a["thinking"][:120]
        if a.get("text"):
            entry["text"] = a["text"]
        actions_brief.append(entry)

    user_msg = (
        f"App: {app}\n"
        f"用户指令: {prompt}\n\n"
        f"VLM 执行动作摘要:\n"
        f"{json.dumps(actions_brief, ensure_ascii=False, indent=2)}\n\n"
        f"已有操作列表:\n"
        f"{json.dumps(existing_operations, ensure_ascii=False)}"
    )

    config = get_config()
    client = OpenAI(
        api_key=config.vision_read.api_key,
        base_url=config.vision_read.base_url,
    )

    try:
        resp = client.chat.completions.create(
            model=config.vision_read.model_name,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=300,
        )
        raw = (resp.choices[0].message.content or "").strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        result = json.loads(raw)
        logger.info(
            f"operation_namer result: {json.dumps(result, ensure_ascii=False)}"
        )
        return result
    except Exception:
        logger.exception("operation_namer: LLM call or parse failed")
        return None
