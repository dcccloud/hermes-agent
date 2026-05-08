"""打开 Facebook 应用"""

from openclaw_agent.engine.operations.facebook.base import FacebookBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep


class FacebookOpenAppOperation(FacebookBaseOperation):
    """打开 Facebook 主页（冷启动，确保从一个干净状态开始）"""

    # Verify by checking app_current().package after start; this is more
    # robust than waiting for a localized UI string (Facebook UI strings
    # vary by language and version).

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device

        # 关闭已运行的 Facebook，确保冷启动
        self.stop_app(device)
        human_sleep(2, 0.5)

        self.logger.info("启动 Facebook...")
        try:
            device.app_start(self.APP_PACKAGE, wait=True)
        except Exception as e:
            return self.failed(f"app_start 调用失败: {e}")

        # 给冷启动留点时间（首屏加载、动画、可能的 splash）
        human_sleep(5, 1)

        current = device.app_current() or {}
        if current.get("package") == self.APP_PACKAGE:
            self.logger.info("✓ Facebook 启动成功")
            return self.success(data={"package": self.APP_PACKAGE})

        return self.failed(
            f"启动失败：当前前台应用为 {current.get('package')!r}，期望 {self.APP_PACKAGE!r}"
        )
