"""进入好省【素材】页签操作"""

import uiautomator2 as u2
from openclaw_agent.engine.operations.haosheng.base import HaoshengBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element


class HaoshengEnterMaterialTabOperation(HaoshengBaseOperation):
    """进入好省【素材】页签"""
    
    MAX_STEPS = 5  # Agent 兜底方案的最大步数
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        
        # 确保应用正在运行
        if not self.ensure_app_running(device):
            return self.failed("好省App未运行")
        
        # 方案 1：优先使用 u2 操作
        u2_result = self._execute_with_u2(device)
        
        if u2_result["success"]:
            return self.success(data={"method": "u2"})
        
        # 方案 2：u2 失败，降级到 agent
        self.logger.warning(f"u2 操作失败: {u2_result['error']}，降级使用 agent")
        return self._execute_with_agent(agent)
    
    def _execute_with_u2(self, device) -> dict:
        """使用 u2 操作进入素材页签"""
        try:
            # 点击"素材"页签
            self.logger.info("导航到素材页面...")
            if not safe_element_click(device(text="素材"), timeout=30, stable_time=1):
                return {"success": False, "error": "未找到【素材】页签"}

            # 验证进入成功（等待端原生元素出现）
            if wait_element(device(text="端原生"), timeout=30, stable_time=0.5):
                self.logger.info("✓ 成功进入素材页签")
                return {"success": True, "message": "成功进入素材页签"}
            else:
                return {"success": False, "error": "素材页签加载异常或【端原生】元素未出现"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent) -> OperationResult:
        """使用 agent 操作进入素材页签（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 进入素材页签
----------------------------------------
🔴 断点现状：位于好省APP内任意页面。
🎯 最终目标：处于【素材】页签。

🗺️ 完整流程路径（Map）：
   [任意页] 点击底部导航栏【素材】 
   → [终点] 素材页（能看到【端原生】筛选）

✅ 执行逻辑：
   1. 【定位】：判断当前是否在素材页。
   2. 【接管】：不在就点底部【素材】；在就完成。
   3. 【完成】：确认进入素材页，回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：仅限好省APP内。
   - 禁区：严禁询问用户。

👉 请立即根据当前屏幕执行接管操作。
"""
            )
            
            self.logger.info(f"Agent 执行结果: {result_message}")
            
            result_lower = result_message.lower()
            
            if any(keyword in result_lower for keyword in ["失败", "错误", "未找到", "无法", "找不到", "max steps"]):
                return self.failed(f"{result_message} (agent 兜底)")
            else:
                return self.success(data={"method": "agent"})
        
        except Exception as e:
            self.logger.error(f"Agent 执行异常: {e}")
            return self.failed(f"agent 兜底失败: {str(e)}")
