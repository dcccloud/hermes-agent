"""申请推广并创建推广跳转到抖音星图操作"""

import time
import uiautomator2 as u2
from openclaw_agent.engine.operations.haosheng.base import HaoshengBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element


class HaoshengApplyAndCreatePromotionOperation(HaoshengBaseOperation):
    """申请推广并创建推广跳转到抖音星图"""
    
    MAX_STEPS = 15  # Agent 兜底方案的最大步数
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        
        # 方案 1：优先使用 u2 操作
        u2_result = self._execute_with_u2(device)
        
        if u2_result["success"]:
            return self.success(data={"target_app": u2_result.get("target_app"), "method": "u2"})
        
        # 方案 2：u2 失败，降级到 agent
        self.logger.warning(f"u2 操作失败: {u2_result['error']}，降级使用 agent")
        return self._execute_with_agent(agent)
    
    def _execute_with_u2(self, device) -> dict:
        """使用 u2 操作申请推广"""
        try:
            self.logger.info("申请推广并创建推广...")

            # 点击申请推广按钮
            if not safe_element_click(device(text="申请推广"), timeout=15):
                return {"success": False, "error": "未找到【申请推广】按钮"}

            # 等待创建推广按钮出现并点击
            if not safe_element_click(device(text="创建推广"), timeout=30, stable_time=0.8):
                return {"success": False, "error": "未找到【创建推广】按钮"}

            # 如果弹出提示框，点击允许和下次不在提醒
            pass

            # 等待包名切换到抖音
            self.logger.info("等待跳转到抖音星图...")
            target_package = "com.ss.android.ugc.aweme"
            timeout = 30
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                current_app = device.app_current()
                current_package = current_app.get("package", "")
                
                if current_package == target_package:
                    self.logger.info(f"✓ 已跳转到抖音，等待星图页面加载...")
                    
                    # 等待页面加载完成，检测两种情况：
                    # 1. 首次参与：出现"参与投稿"文本
                    # 2. 已参与过：出现"任务详情"按钮
                    participate_elem = device(text="参与投稿")
                    task_detail_btn = device(text="任务详情", className="android.widget.Button")
                    
                    # 等待任一元素出现
                    for _ in range(30):  # 30秒超时
                        if participate_elem.exists():
                            self.logger.info("✓ 检测到【参与投稿】元素，首次参与任务")
                            return {"success": True, "message": "成功跳转到星图（首次参与）", "target_app": current_package}
                        elif task_detail_btn.exists():
                            self.logger.info("✓ 检测到【任务详情】按钮，已参与过任务")
                            return {"success": True, "message": "成功跳转到星图（已参与任务）", "target_app": current_package}
                        time.sleep(1)
                    
                    # 超时未找到任何标识元素
                    return {"success": False, "error": "跳转到抖音成功，但未找到【参与投稿】或【任务详情】元素"}
                
                time.sleep(1)
            
            # 超时未跳转
            return {"success": False, "error": f"等待跳转超时，当前应用: {current_package}"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent) -> OperationResult:
        """使用 agent 操作申请推广（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 申请推广并跳转
----------------------------------------
🔴 断点现状：位于好省推广页。
🎯 最终目标：跳转到抖音星图页面。

🗺️ 完整流程路径（Map）：
   [好省推广页] 点击申请推广 -> 点击创建推广 
   → [跳转中] 等待APP切换 
   → [终点] 抖音星图页（有【参与投稿】或【任务详情】）

✅ 执行逻辑：
   1. 【定位】：判断是在申请前、创建前还是已跳转。
   2. 【接管】：逐步点击申请/创建；若已跳转则验证页面。
   3. 【完成】：到达抖音星图页，回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：允许好省->抖音的跳转过程。若跳到其他无关APP，回复"任务失败"。
   - 弹窗：权限弹窗一律允许。
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
