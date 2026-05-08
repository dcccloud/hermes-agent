"""抖音搜索后选择并进入搜索结果

Uses AdaptiveStep: seed RPA (u2 click tab + first result) → VLM fallback.
"""
import random

from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.interaction import safe_element_click, human_sleep


class SelectSearchResultOperation(DouyinBaseOperation):
    """抖音搜索后选择并进入搜索结果第一条"""

    MAX_STEPS = 10
    VALID_TABS = ["视频", "用户", "商品", "店铺"]

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        tab = context.params.get("tab", "视频")

        if tab not in self.VALID_TABS:
            return self.failed(
                f"不支持的 tab: {tab}，仅支持: {', '.join(self.VALID_TABS)}"
            )

        nav_path: list[str] = []
        step = AdaptiveStep("douyin/select_search_result", "click_result")
        if not step.run(
            device, agent,
            rpa_fn=lambda: self._select_with_u2(device, tab),
            vlm_prompt=(
                f"当前在抖音搜索结果页面。请完成以下操作：\n"
                f"1. 点击「{tab}」分类 tab\n"
                f"2. 等待列表加载\n"
                f"3. 点击第一个搜索结果进入详情\n"
                f"完成后回复「完成」。"
            ),
            nav_path=nav_path,
        ):
            return self.failed(f"选择搜索结果 '{tab}' 失败")

        return self.success(
            data={"message": f"选择 '{tab}' 并进入第一个结果"}
        )

    def _select_with_u2(self, device, tab: str) -> bool:
        # 1. Click tab
        tab_btn = device(className="android.widget.Button", text=tab)
        if not safe_element_click(tab_btn, timeout=3, stable_time=0.3):
            return False
        human_sleep(1.0, 0.2)

        # 2. Click first result (coordinate-based)
        width, height = device.window_size()
        x = int(width * random.uniform(0.25, 0.30))
        y = int(height * random.uniform(0.25, 0.30))
        device.click(x, y)
        human_sleep(1.0, 0.2)
        return True
