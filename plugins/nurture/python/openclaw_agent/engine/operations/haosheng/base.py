"""
好省短剧 BaseOperation
封装好省短剧通用操作
"""

import uiautomator2 as u2
from openclaw_agent.engine.core.operation import Operation
from openclaw_agent.engine.utils.interaction import human_sleep, guarantee_click, wait_element


class HaoshengBaseOperation(Operation):
    """好省短剧专属基类"""
    
    APP_PACKAGE = "com.hs.julijuwai.android"
    
    def ensure_app_running(self, device: u2.Device) -> bool:
        """确保好省正在运行"""
        current_app = device.app_current()
        if current_app.get("package") != self.APP_PACKAGE:
            self.logger.warning(f"当前不在好省: {current_app.get('package')}")
            try:
                device.app_start(self.APP_PACKAGE)
                self.wait_page_load(2.0)
                return True
            except Exception as e:
                self.logger.error(f"启动好省失败: {e}")
                return False
        return True
    
    def stop_app(self, device: u2.Device) -> bool:
        """停止好省应用"""
        try:
            if self.APP_PACKAGE in device.app_list_running():
                device.app_stop(self.APP_PACKAGE)
                human_sleep(1.5, 0.3)
                self.logger.info("已停止好省应用")
            return True
        except Exception as e:
            self.logger.error(f"停止好省应用失败: {e}")
            return False
