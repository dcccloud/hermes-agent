"""退出作者主页返回推荐流"""
import time
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation


class ExitAuthorProfileOperation(DouyinBaseOperation):
    """退出作者主页返回推荐流"""
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        
        # 确保在抖音
        if not self.ensure_app_running(device):
            return self.failed("抖音启动失败")
        
        # 按返回键退出主页
        self.logger.info("按返回键退出作者主页")
        device.press("back")
        
        # 等待返回推荐流
        time.sleep(1.0)
        
        return self.success({"message": "已退出作者主页，返回推荐流"})
