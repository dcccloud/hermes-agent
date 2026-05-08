"""导航到抖音数据中心：我 > 创作者中心 > 数据中心

Uses AdaptiveStep for two-tier self-evolving RPA:
  1. Recipe (seed u2 RPA, later replaced by LLM-generated improved version)
  2. VLM fallback (traces are saved for recipe generation)
"""

import time

from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.interaction import safe_element_click


class EnterDataCenterOperation(DouyinBaseOperation):
    """导航路径：底部「我」tab -> 创作者中心 -> 数据中心"""

    MAX_STEPS = 15

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent

        if not self.ensure_app_running(device):
            return self.failed("抖音未运行")

        nav_path: list[str] = []

        # Step 1: go to 「我」tab
        step1 = AdaptiveStep("douyin/enter_data_center", "go_to_me_tab",
                             target_page="profile")
        if not step1.run(
            device, agent,
            rpa_fn=lambda: self._try_click_me_tab(device),
            vlm_prompt=(
                "点击抖音底部导航栏最右侧的「我」tab，进入个人主页。"
                "完成后回复「完成」。"
            ),
            verify_fn=lambda: self._verify_me_page(device),
            nav_path=nav_path,
        ):
            return self.failed("无法进入「我」页面")

        # Step 2: go to 创作者中心
        step2 = AdaptiveStep("douyin/enter_data_center", "go_to_creator_center",
                             target_page="creator_center")
        if not step2.run(
            device, agent,
            rpa_fn=lambda: self._try_click_creator_center(device),
            vlm_prompt=(
                "当前在抖音个人主页。请找到并点击「创作者中心」入口。"
                "它通常在个人主页的中间区域，可能需要稍微下滑才能看到。"
                "点击后等待页面加载，然后回复「完成」。如果找不到回复「未找到」。"
            ),
            verify_fn=lambda: self._verify_creator_center(device),
            nav_path=nav_path,
        ):
            return self.failed("无法进入「创作者中心」")

        # Step 3: go to 数据中心
        step3 = AdaptiveStep("douyin/enter_data_center", "go_to_data_center",
                             target_page="data_center")
        if not step3.run(
            device, agent,
            rpa_fn=lambda: self._try_click_data_center(device),
            vlm_prompt=(
                "当前在抖音创作者中心页面。请找到并点击「数据中心」或「数据概览」入口。"
                "如果需要滑动页面才能看到，请先滑动。"
                "点击后等待页面加载，然后回复「完成」。如果找不到回复「未找到」。"
            ),
            verify_fn=lambda: self._verify_data_center(device),
            nav_path=nav_path,
        ):
            return self.failed("无法进入「数据中心」")

        return self.success(data={"page": "data_center"})

    # -- u2 RPA implementations -------------------------------------------

    def _try_click_me_tab(self, device) -> bool:
        for selector in [
            device(text="我", className="android.widget.TextView"),
            device(descriptionMatches="^我$"),
        ]:
            if selector.exists(timeout=2):
                if safe_element_click(selector, timeout=3, stable_time=0.3):
                    time.sleep(2.0)
                    return True
        return False

    def _try_click_creator_center(self, device) -> bool:
        elem = device(textContains="创作者中心")
        if elem.exists(timeout=3):
            if safe_element_click(elem, timeout=3, stable_time=0.3):
                time.sleep(2.5)
                return True
        return False

    def _try_click_data_center(self, device) -> bool:
        for name in ["数据中心", "数据概览"]:
            elem = device(textContains=name)
            if elem.exists(timeout=2):
                if safe_element_click(elem, timeout=3, stable_time=0.3):
                    time.sleep(2.5)
                    return True
        return False

    # -- verification helpers ----------------------------------------------

    def _verify_me_page(self, device) -> bool:
        indicators = ["关注", "粉丝", "获赞", "抖音号"]
        for text in indicators:
            if device(textContains=text).exists(timeout=1):
                return True
        return False

    def _verify_creator_center(self, device) -> bool:
        indicators = ["数据中心", "数据概览", "创作灵感", "全部服务", "任务中心"]
        for text in indicators:
            if device(textContains=text).exists(timeout=1):
                return True
        return False

    def _verify_data_center(self, device) -> bool:
        indicators = ["总览", "作品分析", "粉丝分析", "直播分析"]
        for text in indicators:
            if device(textContains=text).exists(timeout=1):
                return True
        return False
