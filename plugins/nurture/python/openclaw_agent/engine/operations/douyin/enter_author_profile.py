"""进入作者主页

Uses AdaptiveStep: seed RPA (u2 click avatar) → VLM fallback.
"""
import time
from typing import Any, Dict, Tuple

from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.interaction import safe_element_click


class EnterAuthorProfileOperation(DouyinBaseOperation):
    """进入作者主页"""

    MAX_STEPS = 10

    def check_postcondition(
        self, context: ExecutionContext, op_config: Dict[str, Any],
    ) -> Tuple[bool, str]:
        # 父类无 feed_type postcondition，直接检查页面
        if not self._verify_on_profile(context.device):
            return False, "未成功进入作者主页"
        return True, ""

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent

        nav_path: list[str] = []
        step = AdaptiveStep("douyin/enter_author_profile", "click_avatar",
                            target_page="author_profile")
        if not step.run(
            device, agent,
            rpa_fn=lambda: self._try_click_avatar(device),
            vlm_prompt=(
                "当前在抖音视频播放页面。请点击视频左侧或顶部的作者头像或昵称，"
                "进入作者主页。作者主页特征：能看到作品数、粉丝数、获赞数等信息。"
                "进入后回复「完成」。"
            ),
            verify_fn=lambda: self._verify_on_profile(device),
            nav_path=nav_path,
        ):
            return self.failed("无法进入作者主页")

        return self.success(data={"message": "成功进入作者主页"})

    def _try_click_avatar(self, device) -> bool:
        # ImageView with desc containing 头像
        avatar = device(
            className="android.widget.ImageView",
            descriptionMatches=".*头像.*",
        )
        if not avatar.exists(timeout=2):
            avatar = device(resourceIdMatches=".*avatar.*")
        if not safe_element_click(avatar, timeout=3, stable_time=0.3):
            return False
        time.sleep(1.5)
        return True

    def _verify_on_profile(self, device) -> bool:
        indicators = ["作品", "喜欢", "收藏", "粉丝", "获赞"]
        for text in indicators:
            if device(textContains=text).exists(timeout=1):
                return True
        return False
