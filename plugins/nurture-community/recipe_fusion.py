"""RecipeFusion — LLM-powered recipe fusion engine.

Takes multiple recipe versions from different nodes/devices, analyzes
their strengths, and produces a "best of all" fused version via LLM
synthesis.

Avatar-Hermes: uses ``agent.auxiliary_client`` (Hermes-native) so the
fusion task can be configured with its own provider/model independent
of the main agent — typically a cheap model is fine here. See
``decisions.md`` ADR-001 §recipe-fusion.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from .stores.recipe_store import RecipeEntry

logger = logging.getLogger(__name__)


def _build_fusion_prompt(entries: List[RecipeEntry]) -> str:
    sections: List[str] = []
    for i, e in enumerate(entries):
        stats_lines: List[str] = []
        for nid, ns in e.nodeStats.items():
            total = ns.success + ns.failure
            rate = (ns.success / total * 100) if total else 0
            stats_lines.append(f"  {nid}: {ns.success}/{total} ({rate:.1f}%)")
        stats_block = "\n".join(stats_lines)

        block_parts = [
            f"### Recipe v{i + 1} (origin: {e.originNodeId}, device: {e.originDeviceModel})",
            f"Global success rate: {e.globalSuccessRate * 100:.1f}%",
            f"Sample count: {e.globalSampleCount}",
        ]
        if stats_block:
            block_parts.append(f"Per-node stats:\n{stats_block}")
        block_parts.append(f"```python\n{e.code}\n```")
        sections.append("\n".join(block_parts))

    head = entries[0]
    return "\n".join([
        "你是一个 Android 自动化 RPA 工程师。",
        "",
        f"以下是 **{head.app}/{head.operation}/{head.step}** 的多个 recipe 版本,",
        "来自不同设备/节点。每个版本有各自的成功率统计。",
        "",
        *sections,
        "",
        "## 任务",
        "",
        "请分析每个版本的优劣,融合出一个**取各家之长**的最佳版本。",
        "",
        "## 融合原则",
        "",
        "1. **保留高成功率版本的核心逻辑** — 成功率最高的版本是基础",
        "2. **吸收其他版本的防御性代码** — 如更好的错误处理、状态检测、等待策略",
        "3. **保持兼容性** — 融合版本应在所有设备上都能工作(避免硬编码坐标)",
        "4. **优先使用 u2 元素选择器** — 只在选择器不可用时才使用坐标 fallback",
        "5. **使用 humanize API** — tap_region, swipe_region, wait, detect_page 等",
        "6. **状态感知** — 先 detect_page 确认当前位置",
        "",
        "## 输出要求",
        "",
        "只输出融合后的完整 Python 代码,用 ```python ... ``` 包裹。",
        "函数签名必须是 `def execute(device) -> bool:`。",
    ])


_CODE_BLOCK_RE = re.compile(r"```python\s*\n([\s\S]*?)```")


async def fuse_recipes(
    entries: List[RecipeEntry],
    *,
    auxiliary_task: str = "recipe_fusion",
) -> Optional[str]:
    """Fuse multiple recipe versions into a single canonical version.

    Returns the fused Python source, or None if fewer than 2 versions
    have code, the LLM call fails, or the response is missing the
    expected ``def execute(...)`` signature.

    Uses ``agent.auxiliary_client.get_auxiliary_client(auxiliary_task)``
    so the fusion model can be configured independently of the main
    agent (see ``~/.hermes/config.yaml`` ``auxiliary.recipe_fusion``).
    """
    with_code = [e for e in entries if e.code]
    if len(with_code) < 2:
        logger.debug("recipe_fusion: skipping — fewer than 2 versions with code")
        return None

    prompt = _build_fusion_prompt(with_code)

    try:
        # Lazy import — agent.auxiliary_client lives in the hermes core
        # and we only need it here.
        from agent.auxiliary_client import get_auxiliary_client
    except ImportError as e:  # pragma: no cover — only triggered outside hermes
        logger.warning("recipe_fusion: agent.auxiliary_client unavailable: %s", e)
        return None

    try:
        client = get_auxiliary_client(auxiliary_task)
        response = client.chat.completions.create(
            model=client.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=4000,
        )
    except Exception as e:
        logger.warning("recipe_fusion: LLM call failed: %s", e)
        return None

    raw = ""
    try:
        raw = response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        logger.warning("recipe_fusion: unexpected LLM response shape")
        return None

    match = _CODE_BLOCK_RE.search(raw)
    if not match:
        logger.warning("recipe_fusion: no python code block in LLM response")
        return None

    code = match.group(1).strip()
    if "def execute(" not in code:
        logger.warning("recipe_fusion: fused code missing `def execute(...)`")
        return None

    return code
