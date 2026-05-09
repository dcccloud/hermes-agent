"""抖音触发搜索操作

Uses AdaptiveStep: seed RPA (u2 click search → type → submit) → VLM fallback.
"""

from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.interaction import safe_element_click, human_sleep, wait_element


class TriggerSearchOperation(DouyinBaseOperation):
    """触发搜索操作"""

    MAX_STEPS = 10

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        keyword = context.params.get("search_keyword", "")
        if not keyword:
            return self.failed("搜索内容不能为空")

        nav_path: list[str] = []
        step = AdaptiveStep("douyin/trigger_search", "search_and_submit",
                            target_page="search_results")
        if not step.run(
            device, agent,
            rpa_fn=lambda: self._search_with_u2(device, keyword),
            vlm_prompt=(
                f"当前在抖音页面。请完成搜索操作：\n"
                f"1. 点击页面顶部的搜索按钮\n"
                f"2. 在搜索框中输入「{keyword}」\n"
                f"3. 点击搜索/确认按钮触发搜索\n"
                f"4. 等待搜索结果页加载完成\n"
                f"完成后回复「完成」。"
            ),
            verify_fn=lambda: self._verify_search_results(device),
            nav_path=nav_path,
        ):
            return self.failed(f"搜索 '{keyword}' 失败")

        return self.success(data={"message": f"搜索 '{keyword}' 成功"})

    def _search_with_u2(self, device, keyword: str) -> bool:
        # 1. Click search button
        search_btn = device(
            className="android.widget.Button", description="搜索",
        )
        if not safe_element_click(search_btn, timeout=10, stable_time=0.3):
            return False
        human_sleep(1.0, 0.2)

        # 2. Type keyword
        search_input = device(className="android.widget.EditText")
        if not wait_element(search_input, timeout=3, stable_time=0.3):
            return False
        search_input.set_text(keyword)
        human_sleep(0.5, 0.1)

        # 3. Submit search
        search_trigger = device(
            className="android.widget.TextView", description="搜索",
        )
        if not safe_element_click(search_trigger, timeout=3, stable_time=0.3):
            return False
        human_sleep(1.5, 0.2)
        return True

    def _verify_search_results(self, device) -> bool:
        result_indicator = device(
            className="android.widget.Button", text="综合",
        )
        return result_indicator.exists(timeout=5)
