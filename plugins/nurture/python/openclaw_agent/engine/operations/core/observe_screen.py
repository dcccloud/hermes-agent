"""观察当前界面状态"""
import os
from openclaw_agent.engine.core.operation import Operation, OperationResult, ExecutionContext
from openclaw_agent.engine.utils.oss_helper import upload_image
from openclaw_agent.engine.learning.event_log import emit as emit_event


class ObserveScreenOperation(Operation):
    """观察当前界面状态，获取截图和UI结构"""

    MAX_STEPS = 5

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        params = context.params

        include_xml = params.get("include_xml", False)

        self.logger.info(f"开始观察当前界面状态... (include_xml={include_xml})")

        try:
            # 获取截图
            self.logger.debug("正在截图...")
            screenshot = device.screenshot(format='pillow')

            self.logger.debug(f"截图完成，尺寸: {screenshot.width}x{screenshot.height}")

            # 上传截图到 OSS（用于 event 上报，不返回给 agent）
            screenshot_url = None
            try:
                self.logger.debug("正在上传截图到 OSS...")
                env = os.getenv('APP_ENV', 'dev')
                screenshot_url = upload_image(
                    image_data=screenshot,
                    filename=None,
                    env=env,
                    business_dir='screenshots'
                )
                self.logger.info(f"截图已上传: {screenshot_url}")
            except Exception as upload_err:
                self.logger.warning(f"截图上传失败（不影响操作）: {upload_err}")

            # 通过 event 上报截图 URL（供社区/外部系统使用）
            if screenshot_url:
                emit_event("screenshot.uploaded", {
                    "screenshot_url": screenshot_url,
                    "width": screenshot.width,
                    "height": screenshot.height,
                }, operation="observe_screen")

            # 返回给 agent：包含截图 URL，便于操作者/上层 agent 直接查看屏幕
            result_data = {
                "screen_size": {
                    "width": screenshot.width,
                    "height": screenshot.height
                },
                "screenshot_url": screenshot_url,
            }

            # 仅在明确需要时获取 UI XML
            if include_xml:
                self.logger.debug("正在 dump UI 层级结构...")
                ui_xml = device.dump_hierarchy()
                self.logger.debug(f"UI 层级结构获取完成，大小: {len(ui_xml)} bytes")
                result_data["ui_xml"] = ui_xml
            else:
                self.logger.debug("跳过 UI XML 获取（include_xml=False）")

            self.logger.info("✓ 界面状态观察完成")

            return self.success(result_data)

        except Exception as e:
            self.logger.error(f"观察界面状态异常: {e}")
            return self.failed(f"观察界面状态异常: {str(e)}")
