"""上传视频并发布任务操作[完整流程，保留用于参考]"""

import uiautomator2 as u2
from openclaw_agent.engine.operations.xingtu.base import XingtuBaseOperation
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.utils.interaction import human_sleep, safe_element_click, wait_element
from openclaw_agent.engine.utils.device import is_keyboard_open


class XingtuUploadAndPublishVideoOperation(XingtuBaseOperation):
    """上传视频并发布任务"""
    
    REQUIRED_PARAMS = ["video_title_description_tag"]
    MAX_STEPS = 30  # Agent 兜底方案的最大步数（流程较长）
    
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
        """使用 u2 操作上传视频"""
        try:
            
            self.logger.info("上传视频并发布任务...")

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

            # 7. 选择第一个视频
            first_video = device(description=", 未选中")
            if not safe_element_click(first_video, timeout=10):
                return {"success": False, "error": "未找到第一个视频"}

            # 等待选择音乐元素出现，确认进入视频调整编辑页面
            if not wait_element(device(text="选择音乐"), timeout=15, stable_time=1):
                return {"success": False, "error": "未进入视频发布编辑页面，未找到【选择音乐】元素"}

            # 8. 点击下一步（第一次）
            if not safe_element_click(device(textContains="下一步"), timeout=15):
                return {"success": False, "error": "未找到第一个【下一步】按钮"}

            human_sleep(1)

            # 9. 点击下一步（第二次）
            next_btn = device(text="下一步")
            if not safe_element_click(next_btn, timeout=15):
                return {"success": False, "error": "未找到第二个【下一步】按钮"}

            # 10. 填写发布信息
            self.logger.info("填写发布信息...")
            
            edit_text = device(className="android.widget.EditText")
            if not wait_element(edit_text, timeout=20):
                return {"success": False, "error": "未找到文案输入框"}
            
            edit_text.set_text(video_content)
            self.logger.info(f"已填写标题/文案/标签: {video_content[:30]}...")
            
            human_sleep(0.5, 0.1)
            
            # 11. 收起键盘
            if is_keyboard_open(device):
                self.logger.info("检测到键盘弹出，收起键盘...")
                device.press("back")
                human_sleep(0.3, 0.1)
            
            # 12. 点击高级设置
            if not safe_element_click(device(text="高级设置"), timeout=15):
                return {"success": False, "error": "未找到【高级设置】按钮"}

            # 13. 点击发文助手自主声明
            if not safe_element_click(device(text="发文助手自主声明"), timeout=10):
                return {"success": False, "error": "未找到【发文助手自主声明】按钮"}

            # 14. 选择虚拟演绎仅供娱乐
            if not safe_element_click(device(text="虚构演绎，仅供娱乐"), timeout=10):
                return {"success": False, "error": "未找到【虚拟演绎仅供娱乐】选项"}

            # 15. 退出高级设置
            if not safe_element_click(device(className="android.widget.ImageView", description="返回"), timeout=10):
                return {"success": False, "error": "未找到【返回】按钮"}

            # 16. 点击发布
            if not safe_element_click(device(className="android.widget.FrameLayout", description="发布"), timeout=15):
                return {"success": False, "error": "未找到【发布】按钮"}

            human_sleep(2, 0.3)
            
            # 17. 处理发布后的弹窗提示
            if device(text="以后再说").exists:
                self.logger.info("处理新作品提示弹窗...")
                safe_element_click(device(text="以后再说"), timeout=5)
            
            # 18. 等待发布完成
            self.logger.info("等待视频发布...")
            human_sleep(5, 1)
            if wait_element(device(text="更多"), timeout=60, stable_time=1):
                self.logger.info("✓ 视频发布成功，已跳转到抖音")
                return {"success": True, "message": "视频上传并发布成功"}
            else:
                return {"success": False, "error": "发布超时或未跳转到抖音页面"}
        
        except Exception as e:
            self.logger.error(f"操作异常: {e}")
            return {"success": False, "error": f"操作异常: {str(e)}"}
    
    def _execute_with_agent(self, agent, video_content: str) -> OperationResult:
        """使用 agent 操作上传视频（兜底方案）"""
        try:
            result_message = agent.run(f"""📋 任务卡片：[RPA接管] 全流程发布视频
----------------------------------------
🔴 断点现状：自动化流程中断，需要从当前屏幕接管。
🎯 最终目标：视频发布成功并跳转到播放页。

🗺️ 完整流程路径（Map）：
   [星图任务页] 点击上传视频 
   → [相册选择] 选第一个视频 -> 下一步 
   → [编辑页] 下一步 
   → [发布页] 输入文案"{video_content}" -> 收起键盘 -> 设置"虚构演绎" -> 点击发布 
   → [终点] 抖音视频播放页

✅ 执行逻辑：
   1. 【定位】：观察当前屏幕，判断处于Map中的哪个环节。
   2. 【接管】：从当前位置起，继续向[终点]推进。
   3. 【完成】：到达播放页（看到【更多】按钮），立即回复"任务完成"。

🛡️ 边界与纠错：
   - 范围锁：仅允许在星图上传发布流程内操作。若跳出，回复"任务失败"。
   - 弹窗：遇到权限/提示弹窗，优先点击[允许]/[确认]。
   - 禁区：严禁询问用户。

👉 请立即根据当前屏幕执行接管操作。
"""
            )
            
            self.logger.info(f"Agent 执行结果: {result_message}")
            
            result_lower = result_message.lower()
            
            if any(keyword in result_lower for keyword in ["失败", "错误", "未找到", "无法", "找不到", "被拒绝", "max steps"]):
                return self.failed(f"{result_message} (agent 兜底)")
            else:
                return self.success(data={"method": "agent"})
        
        except Exception as e:
            self.logger.error(f"Agent 执行异常: {e}")
            return self.failed(f"agent 兜底失败: {str(e)}")
