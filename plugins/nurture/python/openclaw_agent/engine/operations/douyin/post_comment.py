"""抖音发布评论

Uses AdaptiveStep: seed RPA (u2 click comment → type → send) → VLM fallback.
"""

from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element


class PostCommentOperation(DouyinBaseOperation):
    """发布评论"""

    REQUIRED_PARAMS = ["comment"]
    MAX_STEPS = 15

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        comment = context.params["comment"]

        nav_path: list[str] = []
        step = AdaptiveStep("douyin/post_comment", "submit_comment")
        if not step.run(
            device, agent,
            rpa_fn=lambda: self._post_with_u2(device, comment),
            vlm_prompt=(
                f"当前在抖音视频播放页面。请完成以下操作：\n"
                f"1. 点击视频右侧的评论按钮（在爱心图标下方）\n"
                f"2. 在评论输入框中输入：{comment}\n"
                f"3. 点击「发送」按钮\n"
                f"4. 等待发送完成后，按返回键收起评论面板\n"
                f"完成后回复「完成」。"
            ),
            nav_path=nav_path,
        ):
            return self.failed("评论失败")

        return self.success(data={"message": f"成功发布评论: {comment}"})

    def _post_with_u2(self, device, comment: str) -> bool:
        # 1. Click comment button
        comment_btn = device(
            className="android.widget.ImageView",
            descriptionMatches="评论[^，]*，按钮",
        )
        if not safe_element_click(comment_btn, timeout=5, stable_time=0.5):
            return False

        # 2. Type comment
        edit = device(className="android.widget.EditText")
        if not wait_element(edit, timeout=3, stable_time=0.3):
            device.press("back")
            return False
        edit.set_text(comment)
        human_sleep(0.5, 0.2)

        # 3. Click send
        send_btn = device(className="android.widget.TextView", text="发送")
        if not safe_element_click(send_btn, timeout=3, stable_time=0.2):
            device.press("back")
            return False
        human_sleep(1.0, 0.3)

        # 4. Close panel
        device.press("back")
        return True
