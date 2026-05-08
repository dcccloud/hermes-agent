"""抖音分享作品到微信

Uses AdaptiveStep: seed RPA (u2 share panel → wechat) → VLM fallback.
Share-to-wechat is a complex cross-app operation, so VLM fallback covers
the full flow.
"""

from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element


class ShareToWechatOperation(DouyinBaseOperation):
    """分享作品给微信好友"""

    REQUIRED_PARAMS = ["friend_name"]
    MAX_STEPS = 25

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        friend_name = context.params["friend_name"]

        nav_path: list[str] = []

        # Step 1: open share panel and select wechat
        step1 = AdaptiveStep("douyin/share_to_wechat", "open_share_panel")
        if not step1.run(
            device, agent,
            rpa_fn=lambda: self._open_share_panel(device),
            vlm_prompt=(
                "当前在抖音视频播放页面。请完成以下操作：\n"
                "1. 点击分享按钮（或更多按钮），打开分享面板\n"
                "2. 找到并点击「分享链接」（可能需要向右滑动）\n"
                "3. 选择「微信」图标\n"
                "等待跳转到微信后回复「完成」。"
            ),
            verify_fn=lambda: self._verify_in_wechat(device),
            nav_path=nav_path,
        ):
            return self.failed("无法打开分享面板或跳转微信")

        # Step 2: find friend and send in wechat
        step2 = AdaptiveStep("douyin/share_to_wechat", "send_to_friend")
        if not step2.run(
            device, agent,
            rpa_fn=lambda: False,  # wechat part has no seed RPA
            vlm_prompt=(
                f"当前在微信页面。请完成以下操作：\n"
                f"1. 在联系人列表或搜索框中找到好友「{friend_name}」\n"
                f"2. 点击进入聊天窗口\n"
                f"3. 长按输入框，选择「粘贴」（剪贴板中有抖音链接）\n"
                f"4. 点击发送按钮\n"
                f"5. 发送成功后，按返回键回到抖音APP\n"
                f"回到抖音后回复「完成」。"
            ),
            nav_path=nav_path,
        ):
            return self.failed(f"微信发送给 {friend_name} 失败")

        return self.success(
            data={"message": f"成功分享给微信好友 {friend_name}"}
        )

    def _open_share_panel(self, device) -> bool:
        # 1. Click share button
        share_btn = device(
            resourceId="com.ss.android.ugc.aweme:id/share_container",
        )
        if not safe_element_click(share_btn, timeout=5, stable_time=0.5):
            return False

        # 2. Find and click "分享链接"
        share_link = device(text="分享链接")
        if not share_link.exists(timeout=2):
            # may need horizontal scroll
            w, h = device.window_size()
            device.swipe(
                int(w * 0.6), int(h * 0.9),
                int(w * 0.3), int(h * 0.9),
                duration=0.3,
            )
            human_sleep(0.5, 0.3)

        if not safe_element_click(share_link, timeout=3, stable_time=0.3):
            device.press("back")
            return False

        # 3. Click wechat
        wechat_btn = device(text="微信")
        if not safe_element_click(wechat_btn, timeout=3, stable_time=0.3):
            device.press("back")
            return False

        human_sleep(2.0, 0.5)
        return True

    def _verify_in_wechat(self, device) -> bool:
        indicators = ["微信", "搜索", "发现", "通讯录"]
        for text in indicators:
            if device(textContains=text).exists(timeout=1):
                return True
        return False
