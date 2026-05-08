"""发布视频操作"""

from openclaw_agent.engine.operations.xingtu.base import XingtuBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element


class XingtuPublishVideoOperation(XingtuBaseOperation):
    """发布视频"""
    
    MAX_STEPS = 8  # Agent 兜底方案的最大步数
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        
        # 方案 1：优先使用 u2 操作
        u2_result = self._execute_with_u2(device)
        
        if u2_result["success"]:
            return self.success(data={"method": "u2"})
        
        # 方案 2：u2 失败，降级到 agent
        self.logger.warning(f"u2 操作失败: {u2_result['error']}，降级使用 agent")
        return self._execute_with_agent(agent)
    
    def _execute_with_u2(self, device) -> dict:
        """使用 u2 操作发布视频"""
        try:
            self.logger.info("发布视频...")
            
            # 1. 点击发布按钮
            if not safe_element_click(device(className="android.widget.FrameLayout", description="发布"), timeout=15):
                return {"success": False, "error": "未找到【发布】按钮"}

            human_sleep(2, 0.3)
            
            # 2. 处理发布后的弹窗提示
            if device(text="以后再说").exists:
                self.logger.info("处理新作品提示弹窗...")
                safe_element_click(device(text="以后再说"), timeout=5)
            
            # 3. 等待发布完成（会自动跳转到抖音播放视频）
            self.logger.info("等待视频发布...")
            human_sleep(5, 1)
            
            if wait_element(device(text="更多"), timeout=60, stable_time=1):
                self.logger.info("✓ 视频发布成功，已跳转到抖音")
                return {"success": True, "message": "视频发布成功"}
            else:
                return {"success": False, "error": "发布超时或未跳转到抖音页面"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent) -> OperationResult:
        """使用 agent 操作发布视频（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 点击发布按钮
----------------------------------------
🔴 断点现状：位于发布页或发布过程中。
🎯 最终目标：等待发布完成进入播放页。

🗺️ 完整流程路径（Map）：
   [发布页] 点击红色发布按钮 
   → [进度条] 等待发布完成 
   → [终点] 抖音视频播放页（有【更多】按钮）

✅ 执行逻辑：
   1. 【定位】：判断是没点发布、正在发布中、还是已发布。
   2. 【接管】：没点就点发布；弹窗点"以后再说"；发布中就等待。
   3. 【完成】：到达播放页，立即回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：仅限发布相关页面。若跳出，回复"任务失败"。
   - 禁区：严禁询问用户。

👉 请立即根据当前屏幕执行接管操作。
"""
            )
            
            self.logger.info(f"Agent 执行结果: {result_message}")
            
            result_lower = result_message.lower()
            
            if any(keyword in result_lower for keyword in ["失败", "错误", "未找到", "无法", "找不到", "超时", "max steps"]):
                return self.failed(f"{result_message} (agent 兜底)")
            else:
                return self.success(data={"method": "agent"})
        
        except Exception as e:
            self.logger.error(f"Agent 执行异常: {e}")
            return self.failed(f"agent 兜底失败: {str(e)}")
