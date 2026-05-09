"""
星图小程序 BaseOperation
封装星图小程序通用操作
"""

import uiautomator2 as u2
from openclaw_agent.engine.core.operation import Operation


class XingtuBaseOperation(Operation):
    """星图小程序专属基类"""
    
    DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"  # 小程序无独立包名，使用抖音包名
    MINIAPP_NAME = "星图短剧发行人计划"
    
    def ensure_in_miniapp(self, device: u2.Device) -> bool:
        """确保在星图小程序内"""
        if self.check_element_exists(device, {"text": self.MINIAPP_NAME}):
            return True
        
        self.logger.warning("当前不在星图小程序")
        return False