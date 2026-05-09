"""配置视频高级设置操作"""

from openclaw_agent.engine.operations.xingtu.base import XingtuBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import safe_element_click
from openclaw_agent.engine.utils.gesture import scroll_down_70percent


class XingtuConfigureVideoSettingsOperation(XingtuBaseOperation):
    """配置视频高级设置"""
    
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
        """使用 u2 操作配置高级设置"""
        try:
            self.logger.info("配置视频高级设置...")
            
            # 1. 向下滚动查看高级设置按钮（通常在页面下方）
            self.logger.info("向下滚动查看页面下方内容...")
            scroll_down_70percent(device)
            
            # 2. 点击高级设置
            if not safe_element_click(device(text="高级设置"), timeout=15):
                return {"success": False, "error": "未找到【高级设置】按钮"}

            # 2. 点击发文助手自主声明
            if not safe_element_click(device(text="发文助手自主声明"), timeout=10):
                return {"success": False, "error": "未找到【发文助手自主声明】按钮"}

            # 3. 选择虚拟演绎仅供娱乐
            if not safe_element_click(device(text="虚构演绎，仅供娱乐"), timeout=10):
                return {"success": False, "error": "未找到【虚构演绎，仅供娱乐】选项"}

            # 4. 退出高级设置
            if not safe_element_click(device(className="android.widget.ImageView", description="返回"), timeout=10):
                return {"success": False, "error": "未找到【返回】按钮"}

            self.logger.info("✓ 高级设置配置完成")
            return {"success": True, "message": "高级设置配置完成"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent) -> OperationResult:
        """使用 agent 操作配置高级设置（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 设置虚构演绎声明
----------------------------------------
🔴 断点现状：在发布页或高级设置弹窗中。
🎯 最终目标：完成设置并返回发布页。

🗺️ 完整流程路径（Map）：
   [发布页] 点击高级设置 
   → [设置弹窗] 点击发文助手自主声明 
   → [声明弹窗] 勾选"虚构演绎，仅供娱乐" 
   → [返回] 关闭弹窗 
   → [终点] 回到发布页

✅ 执行逻辑：
   1. 【定位】：判断是在发布页、设置弹窗还是声明弹窗。
   2. 【接管】：从当前位置继续完成设置。
   3. 【完成】：设置完并关闭弹窗回到发布页后，回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：仅限发布页及弹窗。若跳出，回复"任务失败"。
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
