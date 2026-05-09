"""关注当前视频的作者

Uses AdaptiveStep: seed RPA (u2 find + click follow button) → VLM fallback.
"""
from typing import Any, Dict, Tuple

from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.interaction import safe_element_click


class FollowAuthorOperation(DouyinBaseOperation):
    """关注当前视频的作者"""

    MAX_STEPS = 10

    def check_precondition(
        self, context: ExecutionContext, op_config: Dict[str, Any],
    ) -> Tuple[bool, str]:
        ok, err = super().check_precondition(context, op_config)
        if not ok:
            return ok, err
        status = self.check_follow_status(context.device)
        if status == "already_followed":
            return False, "已关注该作者，跳过"
        return True, ""

    def check_postcondition(
        self, context: ExecutionContext, op_config: Dict[str, Any],
    ) -> Tuple[bool, str]:
        ok, err = super().check_postcondition(context, op_config)
        if not ok:
            return ok, err
        status = self.check_follow_status(context.device)
        if status == "can_follow":
            return False, "关注操作后仍未关注"
        return True, ""

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent

        nav_path: list[str] = []
        step = AdaptiveStep("douyin/follow_author", "click_follow")
        if not step.run(
            device, agent,
            rpa_fn=lambda: self._try_click_follow(device),
            vlm_prompt=(
                "当前在抖音视频播放页面。请找到并点击关注按钮"
                "（通常是作者头像旁的红色加号或「关注」文字）。"
                "注意：优先点击加号按钮而不是头像，点击头像会进入作者主页。"
                "关注成功后回复「完成」。如果已经关注了也回复「完成」。"
            ),
            verify_fn=lambda: self._verify_followed(device),
            nav_path=nav_path,
        ):
            return self.failed("关注失败")

        return self.success(data={"message": "成功关注作者"})

    def _try_click_follow(self, device) -> bool:
        follow_btn = device(
            className="android.widget.Button",
            descriptionMatches=".*关注.*",
        )
        if not follow_btn.exists(timeout=2):
            return False
        return safe_element_click(follow_btn, timeout=3, stable_time=0.3)

    def _verify_followed(self, device) -> bool:
        # 关注成功后加号消失或变为「已关注」
        if device(textContains="已关注").exists(timeout=2):
            return True
        # 加号消失也算成功
        follow_btn = device(
            className="android.widget.Button",
            descriptionMatches=".*关注.*",
        )
        return not follow_btn.exists(timeout=1)
