"""视频发布编辑页填写视频标题、文案、标签等信息操作"""

from openclaw_agent.engine.operations.xingtu.base import XingtuBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, wait_element
from openclaw_agent.engine.utils.device import is_keyboard_open


class XingtuFillVideoInfoOperation(XingtuBaseOperation):
    """视频发布编辑页填写视频标题、文案、标签等信息操作"""
    
    REQUIRED_PARAMS = ["video_title_description_tag"]
    MAX_STEPS = 10  # Agent 兜底方案的最大步数
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        video_content = context.params.get("video_title_description_tag")
        
        # 方案 1：优先使用 u2 操作
        u2_result = self._execute_with_u2(device, video_content)
        
        if u2_result["success"]:
            return self.success(data={"method": "u2"})
        
        # 方案 2：u2 失败，降级到 agent
        self.logger.warning(f"u2 操作失败: {u2_result['error']}，降级使用 agent")
        return self._execute_with_agent(agent, video_content)
    
    def _execute_with_u2(self, device, video_content: str) -> dict:
        """使用 u2 操作填写视频信息"""
        try:
            self.logger.info("填写视频信息...")
            
            # 1. 定位并填写文案输入框
            edit_text = device(className="android.widget.EditText")
            if not wait_element(edit_text, timeout=20):
                return {"success": False, "error": "未找到文案输入框"}
            
            edit_text.set_text(video_content)
            self.logger.info(f"已填写标题/文案/标签: {video_content[:30]}...")
            
            human_sleep(0.5, 0.1)
            
            # 2. 收起键盘
            if is_keyboard_open(device):
                self.logger.info("检测到键盘弹出，收起键盘...")
                device.press("back")
                human_sleep(0.3, 0.1)
            
            self.logger.info("✓ 视频信息填写完成")
            return {"success": True, "message": "视频信息填写完成"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent, video_content: str) -> OperationResult:
        """使用 agent 操作填写视频信息（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 填写视频文案
----------------------------------------
🔴 断点现状：在视频发布编辑页。
🎯 最终目标：文案已输入且键盘已收起。

🗺️ 完整流程路径（Map）：
   [发布页] 点击文案输入框 
   → [键盘弹出] 输入文案"{video_content}" 
   → [收尾] 点击返回/空白处收起键盘 
   → [终点] 键盘已收起的发布页

✅ 执行逻辑：
   1. 【定位】：判断键盘是否弹出，文案是否已输入。
   2. 【接管】：输入文案并确保键盘收起。
   3. 【完成】：确认文案存在且键盘消失，回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：仅限视频发布编辑页。
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
