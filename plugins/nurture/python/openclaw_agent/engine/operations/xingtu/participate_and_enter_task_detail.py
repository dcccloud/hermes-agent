"""参与投稿并进入任务详情页操作"""

import uiautomator2 as u2
from openclaw_agent.engine.operations.xingtu.base import XingtuBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element


class XingtuParticipateAndEnterTaskDetailOperation(XingtuBaseOperation):
    """参与投稿并进入任务详情页"""
    
    MAX_STEPS = 15  # Agent 兜底方案的最大步数
    
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
        """使用 u2 操作参与投稿"""
        try:
            self.logger.info("参与投稿并进入任务详情页...")

            # 检测两种情况：
            # 情况1：已参与任务 - 直接显示"任务详情"按钮（Button类型）
            # 情况2：首次参与 - 显示"参与投稿"按钮
            
            task_detail_btn = device(text="任务详情", className="android.widget.Button")
            participate_btn = device(text="参与投稿")
            
            # 等待任一按钮出现
            for _ in range(15):  # 15秒超时
                if task_detail_btn.exists():
                    self.logger.info("✓ 检测到【任务详情】按钮，已参与过任务，直接进入")
                    # 直接点击任务详情按钮进入详情页
                    task_detail_btn.click()
                    self.logger.info("已点击【任务详情】按钮")
                    human_sleep(0.5, 0.3)
                    
                    # 验证进入任务详情页
                    if wait_element(device(text="上传视频"), timeout=30, stable_time=0.5):
                        self.logger.info("✓ 成功进入任务详情页")
                        return {"success": True, "message": "成功进入任务详情页（已参与任务）"}
                    else:
                        return {"success": False, "error": "点击任务详情后未进入详情页，未找到【上传视频】元素"}
                
                elif participate_btn.exists():
                    self.logger.info("✓ 检测到【参与投稿】按钮，首次参与任务")
                    break
                
                human_sleep(1)
            else:
                return {"success": False, "error": "未找到【参与投稿】或【任务详情】按钮"}

            # 情况2：首次参与任务的完整流程
            # 1. 点击参与投稿按钮
            if not safe_element_click(participate_btn, timeout=5):
                return {"success": False, "error": "无法点击【参与投稿】按钮"}
            
            # 2. 点击已阅读并同意
            if not safe_element_click(device(text="已阅读并同意"), timeout=30):
                return {"success": False, "error": "未找到【已阅读并同意】按钮"}

            # 3. 通过取消按钮的兄弟元素获取参与投稿确认按钮
            cancel_btn = device(text="取消")
            if not wait_element(cancel_btn, timeout=10):
                return {"success": False, "error": "未找到【取消】按钮"}
            
            # 获取索引为1的兄弟元素（弹窗中的参与投稿按钮）
            participate_confirm_btn = cancel_btn.sibling(index=1)
            if not safe_element_click(participate_confirm_btn, timeout=5):
                return {"success": False, "error": "未找到或无法点击弹窗中的【参与投稿】按钮"}
            
            self.logger.info("已点击弹窗中的【参与投稿】按钮")

            # 4. 等待并点击任务详情按钮（参与成功后出现）
            if not wait_element(task_detail_btn, timeout=30, stable_time=1):
                return {"success": False, "error": "未找到【任务详情】Button"}
            
            task_detail_btn.click()
            self.logger.info("已点击【任务详情】按钮")
            human_sleep(0.5, 0.3)

            # 5. 验证进入任务详情页
            if wait_element(device(text="上传视频"), timeout=30, stable_time=0.5):
                self.logger.info("✓ 成功进入任务详情页")
                return {"success": True, "message": "成功进入任务详情页（首次参与任务）"}
            else:
                return {"success": False, "error": "点击任务详情后未进入详情页，未找到【上传视频】元素"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent) -> OperationResult:
        """使用 agent 操作参与投稿（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 进入任务详情页
----------------------------------------
🔴 断点现状：位于星图任务相关页面。
🎯 最终目标：进入任务详情页（能看到"上传视频"按钮）。

🗺️ 完整流程路径（Map）：
   [星图任务页] 
   → 分支A(首次)：点击参与投稿 → 同意协议 → 确认 
   → 分支B(已参与)：点击任务详情 
   → [终点] 任务详情页(有上传视频按钮)

✅ 执行逻辑：
   1. 【定位】：观察是有"参与投稿"还是"任务详情"按钮，或者已在详情页。
   2. 【接管】：若是首次，走分支A；若是已参与，走分支B。
   3. 【完成】：看到"上传视频"按钮，回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：仅限星图任务相关页面。若跳出，回复"任务失败"。
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
