from openclaw_agent.engine.core.operation import Operation, OperationResult, ExecutionContext
from openclaw_agent.engine.operations.core.wake_screen import ensure_screen_on
from openclaw_agent.engine.utils.interaction import human_sleep, wait_element


class EnterRecommendFeedOperation(Operation):
    """打开抖音并进入推荐流"""

    APP_PACKAGE = "com.ss.android.ugc.aweme"

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device

        try:
            # 0. 确保屏幕亮起
            ensure_screen_on(device, self.logger)

            # 1. 停止抖音（清理旧状态，确保干净启动）
            self.logger.info("停止抖音进程...")
            if self.APP_PACKAGE in device.app_list_running():
                device.app_stop(self.APP_PACKAGE)
                human_sleep(1.0, 0.3)
                self.logger.info("已停止抖音进程")

            # 2. 启动抖音（默认进入推荐流）
            self.logger.info("启动抖音APP...")
            device.app_start(self.APP_PACKAGE, wait=True)
            human_sleep(3.0, 0.5)

            self.logger.info("✓ 抖音启动成功，已进入推荐流")
            return self.success(data={"feed": "推荐"})

        except Exception as e:
            self.logger.error(f"启动抖音失败: {e}")
            return self.failed(f"启动抖音失败: {str(e)}")
