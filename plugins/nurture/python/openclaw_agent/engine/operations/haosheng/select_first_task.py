"""选择第一个短剧任务操作"""

import uiautomator2 as u2
from openclaw_agent.engine.operations.haosheng.base import HaoshengBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, wait_element


class HaoshengSelectFirstTaskOperation(HaoshengBaseOperation):
    """选择第一个短剧任务"""
    
    MAX_STEPS = 10  # Agent 兜底方案的最大步数
    
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
        """使用 u2 操作选择第一个任务"""
        try:
            self.logger.info("选择第一个短剧任务...")
            
            # 定位短剧任务列表元素
            task_selector = device(resourceId="com.hs.julijuwai.android:id/iv", className="android.widget.ImageView")
            
            # 等待短剧任务列表加载
            if not wait_element(task_selector, timeout=15, stable_time=0.5):
                return {"success": False, "error": "未找到短剧任务列表"}
            
            # 点击第一个短剧任务
            if task_selector.count > 0:
                task_selector[0].click()
                human_sleep(0.3, 0.1)
                
                # 验证是否进入任务推广页面
                if wait_element(device(text="申请推广"), timeout=30, stable_time=0.5):
                    self.logger.info("✓ 已选择第一个短剧任务并进入推广页面")
                    return {"success": True, "message": "已选择第一个任务"}
                else:
                    return {"success": False, "error": "点击任务后未进入推广页面"}
            else:
                return {"success": False, "error": "短剧任务列表为空"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent) -> OperationResult:
        """使用 agent 操作选择第一个任务（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 选择第一个短剧任务
----------------------------------------
🔴 断点现状：位于好省短剧素材页或详情页。
🎯 最终目标：进入任务详情页（看到【申请推广】按钮）。

🗺️ 完整流程路径（Map）：
   [素材页] 找到短剧列表 -> 点击第一个任务 
   → [终点] 任务详情页

✅ 执行逻辑：
   1. 【定位】：判断是在素材列表页还是已经进了详情页。
   2. 【接管】：在素材页就点第一个任务；已在详情页就完成。
   3. 【完成】：看到【申请推广】按钮，回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：仅限好省素材页和详情页。若跳出，回复"任务失败"。
   - 禁区：严禁询问用户。

👉 请立即根据当前屏幕执行接管操作。
"""
            )
            
            self.logger.info(f"Agent 执行结果: {result_message}")
            
            result_lower = result_message.lower()
            
            if any(keyword in result_lower for keyword in ["失败", "错误", "未找到", "无法", "找不到", "为空", "max steps"]):
                return self.failed(f"{result_message} (agent 兜底)")
            else:
                return self.success(data={"method": "agent"})
        
        except Exception as e:
            self.logger.error(f"Agent 执行异常: {e}")
            return self.failed(f"agent 兜底失败: {str(e)}")
