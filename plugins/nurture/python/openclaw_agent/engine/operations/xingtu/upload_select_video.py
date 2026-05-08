"""上传视频到星图并进入发布页操作（包含选择视频）"""

import uiautomator2 as u2
from openclaw_agent.engine.operations.xingtu.base import XingtuBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element

class XingtuUploadSelectVideoOperation(XingtuBaseOperation):
    """上传视频到星图并进入发布页"""
    
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
        """使用 u2 操作上传并选择视频"""
        try:
            self.logger.info("上传视频并进入发布页...")

            # 1. 点击上传视频按钮
            if not safe_element_click(device(text="上传视频"), timeout=15):
                return {"success": False, "error": "未找到【上传视频】按钮"}

            human_sleep(1, 0.3)

            # 2. 处理重要提示框
            if device(text="重要提示").exists:
                self.logger.info("处理重要提示框...")
                safe_element_click(device(className="android.widget.CheckBox", text="不再提示"), timeout=5)
                if not safe_element_click(device(text="我知道了"), timeout=5):
                    return {"success": False, "error": "重要提示框出现但未找到【我知道了】按钮"}

            human_sleep(0.5, 0.1)

            # 3. 处理摄像权限提示
            if device(text="仅在使用中允许").exists:
                self.logger.info("处理摄像权限提示...")
                if not safe_element_click(device(text="仅在使用中允许"), timeout=5):
                    return {"success": False, "error": "未找到【仅在使用中允许】按钮"}

            # 4. 点击相册
            if not safe_element_click(device(text="相册"), timeout=15):
                return {"success": False, "error": "未找到【相册】按钮"}

            # 5. 处理始终允许权限
            if device(text="始终允许").exists:
                self.logger.info("处理相册权限...")
                if not safe_element_click(device(text="始终允许"), timeout=5):
                    return {"success": False, "error": "未找到【始终允许】按钮"}

            human_sleep(0.5, 0.1)

            # 6. 等待进入相册
            if not wait_element(device(text="所有照片"), timeout=15, stable_time=1):
                return {"success": False, "error": "未进入相册，未找到【所有照片】元素"}

            # 7. 筛选视频
            self.logger.info("筛选视频...")
            # 查找并点击“视频”标签（通常在顶部tab）
            # 尝试通过 text="视频" 查找
            video_tab = device(text="视频")
            if video_tab.exists:
                safe_element_click(video_tab, timeout=5)
                self.logger.info("点击了【视频】筛选标签")
                human_sleep(0.5, 0.2)
            
            # 8. 选择第一个视频
            first_video = device(description=", 未选中")
            if not safe_element_click(first_video, timeout=10):
                return {"success": False, "error": "未找到第一个视频"}

            # 9. 点击下一步（第一次）
            if not safe_element_click(device(textContains="下一步"), timeout=15):
                return {"success": False, "error": "未找到第一个【下一步】按钮"}

            human_sleep(1)

            # 等待剪辑元素出现，确认进入视频调整编辑页面
            if not wait_element(device(text="剪辑"), timeout=15, stable_time=1):
                return {"success": False, "error": "未进入视频发布编辑页面，未找到【剪辑】元素，发布的可能不是视频"}

            # 10. 点击下一步（第二次）
            if not safe_element_click(device(textContains="下一步"), timeout=15):
                return {"success": False, "error": "未找到第二个【下一步】按钮"}

            self.logger.info("✓ 视频选择完成，已进入发布页面")
            return {"success": True, "message": "视频选择完成"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent) -> OperationResult:
        """使用 agent 操作上传并选择视频（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 上传视频并进入发布页
----------------------------------------
🔴 断点现状：自动化流程中断，需要从当前屏幕接管。
🎯 最终目标：进入视频发布编辑页（能看到文案输入框）。

🗺️ 完整流程路径（Map）：
   [星图任务页] 点击上传视频 
   → [相机/拍摄页] 点击相册 
   → [相册列表] 选第一个视频(打钩) -> 点击下一步 
   → [视频编辑页] 点击下一步 
   → [终点] 视频发布编辑页

✅ 执行逻辑：
   1. 【定位】：观察当前屏幕，判断处于Map中的哪个位置。
   2. 【接管】：从当前位置起，继续向[终点]推进。
   3. 【完成】：一旦到达视频发布页，立即回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：仅允许在星图上传流程涉及的5个页面内操作。若跳出（如回到桌面），回复"任务失败"。
   - 弹窗：遇到权限/引导弹窗，优先点击[允许]/[确认]。
   - 禁区：严禁询问用户。

👉 请立即根据当前屏幕执行接管操作。
""")
            
            self.logger.info(f"Agent 执行结果: {result_message}")
            
            result_lower = result_message.lower()
            
            if any(keyword in result_lower for keyword in ["失败", "错误", "未找到", "无法", "找不到", "被拒绝", "max steps"]):
                return self.failed(f"{result_message} (agent 兜底)")
            else:
                return self.success(data={"method": "agent"})
        
        except Exception as e:
            self.logger.error(f"Agent 执行异常: {e}")
            return self.failed(f"agent 兜底失败: {str(e)}")
