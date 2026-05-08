"""搜索指定剧目操作"""

import uiautomator2 as u2
from openclaw_agent.engine.operations.haosheng.base import HaoshengBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element


class HaoshengSearchDramaOperation(HaoshengBaseOperation):
    """搜索指定剧目"""
    
    REQUIRED_PARAMS = ["drama_name"]
    MAX_STEPS = 10  # Agent 兜底方案的最大步数
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        drama_name = context.params.get("drama_name")
        
        # 方案 1：优先使用 u2 操作
        u2_result = self._execute_with_u2(device, drama_name)
        
        if u2_result["success"]:
            return self.success(data={"drama_name": drama_name, "method": "u2"})
        
        # 方案 2：u2 失败，降级到 agent
        self.logger.warning(f"u2 操作失败: {u2_result['error']}，降级使用 agent")
        return self._execute_with_agent(agent, drama_name)
    
    def _execute_with_u2(self, device, drama_name: str) -> dict:
        """使用 u2 操作搜索剧目"""
        try:
            self.logger.info(f"搜索剧目: {drama_name}")
            
            # 定位搜索输入框
            search_input = device(text="输入剧名搜一搜", className="android.widget.EditText")
            if not wait_element(search_input, timeout=15, stable_time=0.5):
                return {"success": False, "error": "未找到搜索输入框"}

            # 输入剧名（set_text 会自动点击获取焦点）
            search_input.set_text(drama_name)
            human_sleep(0.5, 0.2)
            self.logger.info(f"已输入剧名: {drama_name}")

            # 点击搜索按钮
            if not safe_element_click(device(text="搜索"), timeout=10):
                return {"success": False, "error": "未找到搜索按钮"}

            # 等待搜索结果加载
            if wait_element(device(textContains=drama_name), timeout=20, stable_time=1):
                self.logger.info(f"✓ 搜索到剧目: {drama_name}")
                return {"success": True, "message": f"搜索到剧目: {drama_name}"}
            else:
                return {"success": False, "error": f"搜索结果中未找到'{drama_name}'"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent, drama_name: str) -> OperationResult:
        """使用 agent 操作搜索剧目（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 搜索短剧
----------------------------------------
🔴 断点现状：位于好省素材页。
🎯 最终目标：搜索结果加载完成。

🗺️ 完整流程路径（Map）：
   [素材页] 点击搜索框 
   → [输入状态] 输入剧名"{drama_name}" -> 点击搜索按钮 
   → [终点] 搜索结果页

✅ 执行逻辑：
   1. 【定位】：判断搜索框状态（未激活/已激活/已搜索）。
   2. 【接管】：激活输入框 -> 输入内容 -> 点击搜索。
   3. 【完成】：看到搜索结果列表，回复"任务完成"。

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
            elif any(keyword in result_lower for keyword in ["成功", "完成", "已搜索", "搜索到"]):
                return self.success(data={"drama_name": drama_name, "method": "agent"})
            else:
                return self.success(data={"drama_name": drama_name, "method": "agent"})
        
        except Exception as e:
            self.logger.error(f"Agent 执行异常: {e}")
            return self.failed(f"agent 兜底失败: {str(e)}")
