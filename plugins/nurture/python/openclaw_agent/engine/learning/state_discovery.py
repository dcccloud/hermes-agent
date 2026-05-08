"""Vision-LLM-based state discovery for unknown pages.

When an operation reaches a page that detect_page() cannot identify,
this module takes a screenshot and uses the vision_read LLM to:
1. Determine if it's a genuinely new state or a known one
2. If new: assign a name, description, and indicator texts
3. Register the new state in the AppGraph

See DESIGN.md section 2.4 for the full algorithm.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from openclaw_agent.engine.common.logger import get_logger

from openclaw_agent.engine.learning.vision_utils import (
    take_screenshot_b64,
    vision_read_b64,
)

logger = get_logger("state_discovery")


@dataclass
class DiscoveryResult:
    """Result of a state discovery analysis."""

    state_id: str
    name: str
    description: str
    indicators: list[str] = field(default_factory=list)
    is_optional: bool = False
    is_new: bool = True


def check_stability(device: Any, wait_seconds: float = 1.5) -> bool:
    """Check if the current page is stable (not a transient popup/loading).

    Takes two screenshots separated by wait_seconds and compares their
    size.  If the page changes significantly, it's transient.
    """
    try:
        img1 = device.screenshot(format="pillow")
        time.sleep(wait_seconds)
        img2 = device.screenshot(format="pillow")

        if img1.size != img2.size:
            return False

        # Sample a grid of pixels to detect significant changes
        w, h = img1.size
        changed = 0
        total = 0
        step = max(w, h) // 20
        for x in range(0, w, step):
            for y in range(0, h, step):
                p1 = img1.getpixel((x, y))
                p2 = img2.getpixel((x, y))
                total += 1
                diff = sum(abs(a - b) for a, b in zip(p1[:3], p2[:3]))
                if diff > 60:
                    changed += 1

        ratio = changed / total if total > 0 else 0
        stable = ratio < 0.5
        label = "stable" if stable else "transient"
        logger.debug(f"Stability check: {ratio * 100:.1f}% pixels changed → {label}")
        return stable
    except Exception:
        logger.debug("Stability check failed, assuming stable")
        return True


def discover_state(
    device: Any,
    graph: Any,
    nav_path: list[str],
    vlm_context: str = "",
) -> DiscoveryResult | None:
    """Analyze the current screen to identify/name an unknown state.

    Takes a screenshot and sends it to vision_read LLM with:
    - The screenshot
    - The existing state graph (for context)
    - The navigation path that led here
    - (optional) VLM finish message describing what it just did

    Returns a DiscoveryResult, or None if analysis fails.
    """
    try:
        img_b64 = take_screenshot_b64(device)
    except Exception:
        logger.exception("Failed to capture screenshot for state discovery")
        return None

    graph_summary = _build_graph_summary(graph)
    path_str = " → ".join(nav_path) if nav_path else "(未知路径)"

    prompt = _build_discovery_prompt(graph_summary, path_str,
                                     vlm_context=vlm_context)

    try:
        response = vision_read_b64(img_b64, prompt)
        return _parse_response(response)
    except Exception:
        logger.exception("State discovery LLM call failed")
        return None


def discover_state_from_capture(
    img_b64: str,
    graph: Any,
    nav_path: list[str],
) -> DiscoveryResult | None:
    """Like discover_state but uses a pre-captured screenshot.

    Called by the async discovery consumer — the screenshot was taken at
    the moment the unknown page was encountered, not at consumption time.
    """
    graph_summary = _build_graph_summary(graph)
    path_str = " → ".join(nav_path) if nav_path else "(未知路径)"
    prompt = _build_discovery_prompt(graph_summary, path_str)

    try:
        response = vision_read_b64(img_b64, prompt)
        return _parse_response(response)
    except Exception:
        logger.exception("State discovery (from capture) LLM call failed")
        return None


def _build_graph_summary(graph: Any) -> str:
    """Build a concise summary of the existing graph for the prompt."""
    pages = graph.get_pages()
    if not pages:
        return "(空图，尚无已知状态)"
    lines = []
    for pid, state in pages.items():
        ind_texts = [i["text"] for i in state.indicators]
        desc_part = f" — {state.description}" if state.description else ""
        opt = " [弹窗]" if state.is_optional else ""
        neighbors = list(state.transitions.keys()) if state.transitions else []
        neighbor_part = f"\n  相邻节点：{', '.join(neighbors)}" if neighbors else ""
        lines.append(
            f"- {pid}（{state.name}）{opt}{desc_part}\n"
            f"  识别标志：{', '.join(ind_texts)}{neighbor_part}"
        )
    return "\n".join(lines)


def _build_discovery_prompt(graph_summary: str, path_str: str,
                            vlm_context: str = "") -> str:
    vlm_hint = ""
    if vlm_context:
        vlm_hint = (
            f"\n## VLM 上下文\n"
            f"VLM 刚完成了一项操作任务，它认为当前状态是：{vlm_context}\n"
            f"请结合这个上下文和截图来判断当前页面。\n"
        )

    return f"""你是一个 APP 页面状态分析师。

## 任务
分析截图中的页面，判断它是一个新页面还是已知页面。
{vlm_hint}
## 到达路径
{path_str}

## 已知状态图
{graph_summary}

## 判断规则
1. **状态 = UI 布局结构**，不是内容。同一布局不同内容 = 同一状态
2. 页面内滚动不算新状态
3. 弹窗/广告/确认框等临时覆盖层标记为 optional
4. 如果看起来和已知状态非常相似但到达路径不同，给一个类似但明确不同的名字

## indicator 选择原则（极重要）
indicators 是用于自动化识别页面的关键文本。选择时必须：
1. **避免与相邻/兄弟页面共享**：仔细看已知状态图中的 indicator，你选的 indicator 必须是**本页面独有**的文本，不能出现在父页面、子页面、或同级 tab 页面上
2. **避免公共导航文本**：不要选 tab 栏标签（如"总览""作品分析"），因为同一组 tab 下所有子页面都有这些文本
3. **选内容区域独有的文本**：选该页面内容区域中特有的标题、指标名、按钮文字等，只有在这个页面才会出现
4. **至少 3 个 indicator**，threshold 建议 2

## 输出格式
返回 JSON（不要其他文字）：
```json
{{
  "is_new": true,
  "state_id": "英文标识符_用下划线",
  "name": "简短中文名称",
  "description": "一句话描述这个页面的功能和内容",
  "indicators": ["本页独有文本1", "本页独有文本2", "本页独有文本3"],
  "is_optional": false
}}
```
如果是已知状态，`is_new` 设为 false，`state_id` 填已知的 ID，其他字段可以省略。"""


def _parse_response(response: str) -> DiscoveryResult | None:
    """Parse the LLM JSON response into a DiscoveryResult."""
    # Extract JSON from response (may have markdown fencing)
    text = response.strip()
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse discovery response as JSON: {text[:200]}")
        return None

    is_new = data.get("is_new", True)
    state_id = data.get("state_id", "")
    if not state_id:
        logger.warning("Discovery response missing state_id")
        return None

    return DiscoveryResult(
        state_id=state_id,
        name=data.get("name", state_id),
        description=data.get("description", ""),
        indicators=data.get("indicators", []),
        is_optional=data.get("is_optional", False),
        is_new=is_new,
    )
