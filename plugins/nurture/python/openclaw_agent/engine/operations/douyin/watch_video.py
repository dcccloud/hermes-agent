"""观看当前视频指定时长"""
import time
import random
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation


class WatchVideoOperation(DouyinBaseOperation):
    """观看当前视频指定时长"""
    
    REQUIRED_PARAMS = ["duration_seconds"]
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        duration = context.params.get("duration_seconds", 10)
        
        # 确保在抖音
        if not self.ensure_app_running(device):
            return self.failed("抖音启动失败")
        
        # 添加随机微调（±1-2秒），更加拟人
        actual_duration = duration + random.uniform(-1, 2)
        # 限制在合理范围（3-90秒）
        actual_duration = max(3, min(90, actual_duration))
        
        self.logger.info(f"观看视频 {actual_duration:.1f} 秒")
        time.sleep(actual_duration)
        
        return self.success({
            "duration": actual_duration,
            "message": f"已观看视频 {actual_duration:.1f} 秒"
        })
