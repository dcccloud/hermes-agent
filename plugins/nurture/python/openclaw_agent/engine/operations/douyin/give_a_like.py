"""抖音点赞操作

Uses AdaptiveStep: seed RPA (double-tap screen center) → VLM fallback.
"""
import random
from typing import Any, Dict, Tuple

from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.interaction import human_sleep


class DouyinGiveALikeOperation(DouyinBaseOperation):
    """双击屏幕中心点赞，VLM 兜底点击爱心按钮"""

    MAX_STEPS = 8

    def check_precondition(
        self, context: ExecutionContext, op_config: Dict[str, Any],
    ) -> Tuple[bool, str]:
        ok, err = super().check_precondition(context, op_config)
        if not ok:
            return ok, err
        status = self.check_like_status(context.device)
        if status == "liked":
            return False, "当前视频已点赞，跳过"
        return True, ""

    def check_postcondition(
        self, context: ExecutionContext, op_config: Dict[str, Any],
    ) -> Tuple[bool, str]:
        ok, err = super().check_postcondition(context, op_config)
        if not ok:
            return ok, err
        status = self.check_like_status(context.device)
        if status == "not_liked":
            return False, "点赞操作后仍未点赞"
        return True, ""

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent

        nav_path: list[str] = []
        step = AdaptiveStep("douyin/give_a_like", "tap_like")
        if not step.run(
            device, agent,
            rpa_fn=lambda: self._double_tap_like(device),
            vlm_prompt=(
                "当前在抖音视频播放页面。请点击视频右侧的爱心图标（点赞按钮），"
                "使其变为红色。注意区分视频页（爱心在右侧中间）和直播页"
                "（爱心在底部），只操作视频页的爱心。"
                "点赞成功后回复「完成」。"
            ),
            nav_path=nav_path,
        ):
            return self.failed("点赞失败")

        return self.success(data={"message": "成功点赞视频"})

    def _double_tap_like(self, device) -> bool:
        """双击屏幕中心区域点赞（拟人化随机坐标）"""
        width, height = device.window_size()

        # 点击区域随机偏移，模拟不同手持姿势
        cx = random.uniform(0.42, 0.48)
        cy = random.uniform(0.55, 0.60)
        rx = random.uniform(0.13, 0.17)
        ry = random.uniform(0.06, 0.09)

        tap_x = random.randint(int(width * (cx - rx)), int(width * (cx + rx)))
        tap_y = random.randint(int(height * (cy - ry)), int(height * (cy + ry)))

        device.double_click(tap_x, tap_y)
        human_sleep(0.8, 0.2)
        return True
