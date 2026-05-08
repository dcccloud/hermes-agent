"""
Facebook BaseOperation — shared helpers for facebook-app handlers.
"""

import uiautomator2 as u2
from openclaw_agent.engine.core.operation import Operation
from openclaw_agent.engine.utils.interaction import human_sleep


class FacebookBaseOperation(Operation):
    """Facebook 专属基类"""

    APP_PACKAGE = "com.facebook.katana"

    def ensure_app_running(self, device: u2.Device) -> bool:
        current_app = device.app_current()
        if current_app.get("package") != self.APP_PACKAGE:
            self.logger.warning(f"当前不在 Facebook: {current_app.get('package')}")
            try:
                device.app_start(self.APP_PACKAGE)
                human_sleep(2, 0.5)
                return True
            except Exception as e:
                self.logger.error(f"启动 Facebook 失败: {e}")
                return False
        return True

    def stop_app(self, device: u2.Device) -> bool:
        try:
            if self.APP_PACKAGE in device.app_list_running():
                device.app_stop(self.APP_PACKAGE)
                human_sleep(1.5, 0.3)
                self.logger.info("已停止 Facebook 应用")
            return True
        except Exception as e:
            self.logger.error(f"停止 Facebook 应用失败: {e}")
            return False
