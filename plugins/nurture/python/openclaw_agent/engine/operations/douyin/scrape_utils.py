"""数据中心采集共用工具

架构原则：
- 「动作」用 u2 (优先) / agent (原子操作兜底)
- 「读屏」用 device.screenshot() + GPT vision API
- agent 不做复合操作、不做数据提取
- 调用者传入 schema 定义想要什么数据，operation 尽量填充
"""
import json
import logging
import re
import time
from typing import Any, Optional

from openclaw_agent.engine.learning.vision_utils import (
    take_screenshot_b64 as _take_screenshot_b64,
    vision_read,
)
from openclaw_agent.engine.utils.gesture import scroll_down_70percent
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema-driven page scraper
# ---------------------------------------------------------------------------

def _build_extraction_prompt(schema: dict[str, str]) -> str:
    """从 schema 生成 vision 提取 prompt"""
    fields = "\n".join(f'  "{k}": "{desc}"' for k, desc in schema.items())
    return (
        "请仔细阅读截图中的内容，提取以下字段的数据，以 JSON 格式返回。\n\n"
        f"需要提取的字段：\n{{\n{fields}\n}}\n\n"
        "规则：\n"
        "- 只返回一个 JSON 对象，不要其他文字\n"
        "- 如果某个字段在截图中找不到，值设为 null\n"
        "- 数字保持原样（如 '1.2万' 返回 '1.2万'，不要转换）\n"
        "- 百分比保持原样（如 '35.2%'）\n"
        "- 看到什么写什么，不要编造数据\n"
    )


def _parse_json_response(text: str) -> dict[str, Any] | list:
    """从 GPT 响应中提取 JSON（对象或数组），容忍 markdown code block 包裹"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 有时 vision 模型在 JSON 前后夹杂额外文字，尝试提取首个 JSON 块
        for pattern in [r"\[.*\]", r"\{.*\}"]:
            m2 = re.search(pattern, text, re.DOTALL)
            if m2:
                try:
                    return json.loads(m2.group(0))
                except json.JSONDecodeError:
                    continue
        logger.warning(f"JSON 解析失败，原始文本: {text[:300]}")
        return {}


def scrape_page(
    device,
    schema: dict[str, str],
    max_screens: int = 4,
    scroll_wait: float = 1.5,
) -> dict[str, Any]:
    """
    通用页面采集：逐屏截图 + GPT vision 按 schema 提取数据。

    Args:
        device: u2 设备
        schema: {"field_name": "字段描述", ...}
        max_screens: 最大滚屏次数
        scroll_wait: 滚屏后等待秒数

    Returns:
        提取到的数据 dict，字段名与 schema key 对应
    """
    prompt = _build_extraction_prompt(schema)
    # 先用 None 初始化所有字段，区分「没采到」(None) 和「值为零」(0)
    merged: dict[str, Any] = {k: None for k in schema}
    prev_raw: Optional[str] = None

    for i in range(max_screens):
        try:
            raw = vision_read(device, prompt)
        except Exception as e:
            logger.error(f"Vision 读取第 {i + 1} 屏异常: {e}")
            break

        if prev_raw and raw == prev_raw:
            logger.info(f"第 {i + 1} 屏与上一屏相同，判定已到底")
            break

        data = _parse_json_response(raw)
        # 合并：只填充尚未取到的字段
        for k, v in data.items():
            if v is not None and (k not in merged or merged[k] is None):
                merged[k] = v

        logger.info(f"已采集第 {i + 1}/{max_screens} 屏, 当前 {len([v for v in merged.values() if v is not None])}/{len(schema)} 字段")
        prev_raw = raw

        # 所有字段都已填充则提前结束
        if all(merged.get(k) is not None for k in schema):
            logger.info("所有字段已填充，提前结束采集")
            break

        scroll_down_70percent(device)
        human_sleep(scroll_wait, 0.3)

    return merged


def scrape_list_page(
    device,
    item_schema: dict[str, str],
    max_items: int = 10,
    max_screens: int = 6,
    scroll_wait: float = 1.5,
) -> list[dict[str, Any]]:
    """
    列表页采集：逐屏截图 + GPT vision 提取列表数据（如作品列表）。

    Args:
        device: u2 设备
        item_schema: 每条数据的 schema，如 {"title": "标题", "plays": "播放量"}
        max_items: 最多采集多少条
        max_screens: 最大滚屏次数
        scroll_wait: 滚屏后等待秒数

    Returns:
        列表，每条为 schema 对应的 dict
    """
    fields = "\n".join(f'    "{k}": "{desc}"' for k, desc in item_schema.items())
    prompt = (
        "请仔细阅读截图中的列表内容，提取所有可见的条目。\n"
        "以 JSON 数组格式返回，每条数据包含以下字段：\n\n"
        f"[\n  {{\n{fields}\n  }}\n]\n\n"
        "规则：\n"
        "- 只返回一个 JSON 数组，不要其他文字\n"
        "- 数字保持原样（如 '1.2万'）\n"
        "- 看不到的字段设为 null\n"
        "- 只返回截图中看到的条目，不要编造\n"
    )

    all_items: list[dict] = []
    seen_keys: set[str] = set()
    prev_raw: Optional[str] = None

    for i in range(max_screens):
        if len(all_items) >= max_items:
            break

        try:
            raw = vision_read(device, prompt)
        except Exception as e:
            logger.error(f"Vision 读取第 {i + 1} 屏异常: {e}")
            break

        if prev_raw and raw == prev_raw:
            logger.info(f"第 {i + 1} 屏与上一屏相同，判定已到底")
            break

        parsed = _parse_json_response(raw)
        items = parsed if isinstance(parsed, list) else parsed.get("items", [])

        for item in items:
            if not isinstance(item, dict) or not item:
                continue
            # 去重：用所有非 null 值的组合
            dedup_vals = tuple(str(v) for v in item.values() if v is not None)
            if dedup_vals in seen_keys:
                continue
            seen_keys.add(dedup_vals)
            all_items.append(item)

        logger.info(f"已采集第 {i + 1}/{max_screens} 屏, 当前 {len(all_items)} 条")
        prev_raw = raw

        if len(all_items) >= max_items:
            break

        scroll_down_70percent(device)
        human_sleep(scroll_wait, 0.3)

    return all_items[:max_items]


# ---------------------------------------------------------------------------
# 原子动作：点击 tab / 元素 (u2 优先，agent 原子兜底)
# ---------------------------------------------------------------------------

def click_tab(device, agent, tab_name: str, timeout: float = 3) -> bool:
    """点击页面中的 tab，u2 优先，agent 原子兜底。"""
    elem = device(text=tab_name)
    if elem.exists(timeout=timeout):
        if safe_element_click(elem, timeout=3, stable_time=0.3):
            logger.info(f"u2 点击 tab '{tab_name}' 成功")
            human_sleep(1.5, 0.3)
            return True

    elem2 = device(textContains=tab_name)
    if elem2.exists(timeout=1):
        if safe_element_click(elem2, timeout=3, stable_time=0.3):
            logger.info(f"u2 点击 tab '{tab_name}' 成功 (textContains)")
            human_sleep(1.5, 0.3)
            return True

    try:
        agent.run(
            f"请点击页面上的「{tab_name}」标签。"
            "点击后回复「完成」，找不到回复「未找到」。"
        )
        logger.info(f"agent 点击 tab '{tab_name}' 成功")
        human_sleep(1.5, 0.3)
        return True
    except Exception as e:
        logger.error(f"agent 点击 tab '{tab_name}' 异常: {e}")

    logger.warning(f"无法点击 tab '{tab_name}'")
    return False


def click_element(device, agent, text: str, timeout: float = 3) -> bool:
    """点击页面中包含指定文字的元素，u2 优先，agent 原子兜底。"""
    elem = device(textContains=text)
    if elem.exists(timeout=timeout):
        if safe_element_click(elem, timeout=3, stable_time=0.3):
            logger.info(f"u2 点击 '{text}' 成功")
            human_sleep(1.5, 0.3)
            return True

    try:
        agent.run(
            f"请点击页面上包含「{text}」文字的按钮。"
            "点击后回复「完成」，找不到回复「未找到」。"
        )
        logger.info(f"agent 点击 '{text}' 成功")
        human_sleep(1.5, 0.3)
        return True
    except Exception as e:
        logger.error(f"agent 点击 '{text}' 异常: {e}")

    return False


# ---------------------------------------------------------------------------
# 文本解析工具（保留给需要的地方）
# ---------------------------------------------------------------------------

def parse_count(text: str) -> int:
    """将 '1.2万' / '12.3w' / '3亿' / '1,234' 等解析为整数"""
    if not text:
        return 0
    if isinstance(text, (int, float)):
        return int(text)
    text = str(text).strip().replace(",", "").replace(" ", "")
    m = re.match(r"([\d.]+)\s*[万wW]", text)
    if m:
        return int(float(m.group(1)) * 10_000)
    m = re.match(r"([\d.]+)\s*[亿]", text)
    if m:
        return int(float(m.group(1)) * 100_000_000)
    m = re.match(r"[\d.]+", text)
    if m:
        return int(float(m.group(0)))
    return 0
