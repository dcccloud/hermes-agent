"""筛选端原生剧目操作"""

import uiautomator2 as u2
from openclaw_agent.engine.operations.haosheng.base import HaoshengBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element


class HaoshengFilterNativeDramaOperation(HaoshengBaseOperation):
    """筛选端原生剧目"""
    
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
        """使用 u2 操作筛选端原生剧目"""
        try:
            self.logger.info("筛选端原生剧目...")

            # 点击端原生筛选按钮
            if not safe_element_click(device(text="端原生"), timeout=15):
                return {"success": False, "error": "未找到【端原生】筛选按钮"}

            # 等待筛选结果加载
            human_sleep(2, 0.3)

            self.logger.info("✓ 端原生筛选完成")
            return {"success": True, "message": "端原生筛选完成"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent) -> OperationResult:
        """使用 agent 操作筛选端原生剧目（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 筛选端原生剧目
----------------------------------------
🔴 断点现状：位于好省素材页。
🎯 最终目标：【端原生】按钮处于选中（高亮）状态。

🗺️ 完整流程路径（Map）：
   [素材页] 找到筛选区 -> 点击【端原生】 
   → [终点] 筛选结果已更新

✅ 执行逻辑：
   1. 【定位】：观察【端原生】按钮是否高亮。
   2. 【接管】：若未高亮，点击它；若已高亮，直接完成。
   3. 【完成】：确认按钮已选中，回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：仅限好省素材页。
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
