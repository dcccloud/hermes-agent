"""打开好省App操作"""

import uiautomator2 as u2
from openclaw_agent.engine.operations.haosheng.base import HaoshengBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, wait_element


class HaoshengOpenAppOperation(HaoshengBaseOperation):
    """打开好省App"""
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        
        # 关闭已运行的应用
        self.stop_app(device)
        human_sleep(3)

        # 启动应用
        self.logger.info("启动好省App...")
        device.app_start(self.APP_PACKAGE, wait=True)
        human_sleep(5, 1)
        
        # 验证启动成功：等待素材元素出现
        if wait_element(device(text="素材"), timeout=40, stable_time=0.5):
            self.logger.info("✓ 好省App启动成功")
            return self.success(data={"package": self.APP_PACKAGE})
        else:
            return self.failed("启动失败，未找到【素材】元素")
