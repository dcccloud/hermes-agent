"""采集数据中心「作品分析」分页数据

调用者可通过 params.item_schema 覆盖每条作品的字段定义。
operation 职责：切到作品分析 tab → 逐屏截图 + vision 提取列表 → 去重合并
"""
import time
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.operations.douyin.scrape_utils import (
    click_tab,
    scrape_list_page,
    vision_read,
)
from openclaw_agent.engine.utils.gesture import scroll_up_70percent

DEFAULT_ITEM_SCHEMA: dict[str, str] = {
    "title": "作品标题或描述文字",
    "publish_time": "发布时间，如 03-15 或 2026-03-15",
    "plays": "播放量",
    "likes": "点赞数",
    "comments": "评论数",
    "shares": "分享数/转发数",
}


class ScrapeContentAnalysisOperation(DouyinBaseOperation):
    """采集数据中心「作品分析」：schema-driven 列表提取"""

    MAX_STEPS = 25

    def _ensure_on_content_tab(self, device, agent) -> bool:
        """切换到「作品分析」分页并验证"""
        # 先滚到顶部确保 tab 栏可见
        for _ in range(2):
            scroll_up_70percent(device)
            time.sleep(0.5)

        for attempt in range(3):
            click_tab(device, agent, "作品分析")
            time.sleep(2)

            # 用 vision 验证当前是否在作品分析页
            try:
                check = vision_read(
                    device,
                    "当前页面是否显示「作品分析」列表（包含视频标题、播放量等数据）？"
                    "只回答 yes 或 no，不要其他文字。",
                )
                if "yes" in check.lower():
                    self.logger.info(f"第 {attempt + 1} 次验证: 已在作品分析页")
                    return True
                self.logger.warning(f"第 {attempt + 1} 次验证: 不在作品分析页 ({check})")
            except Exception as e:
                self.logger.error(f"验证截图异常: {e}")

            # 没切成功，先滚回顶部再重试
            scroll_up_70percent(device)
            time.sleep(1)

        return False

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        max_items = context.params.get("max_items", 10)
        item_schema = context.params.get("item_schema", DEFAULT_ITEM_SCHEMA)

        if not self._ensure_on_content_tab(device, agent):
            return self.failed("无法切换到「作品分析」分页")

        est_screens = max(3, (max_items // 3) + 2)
        items = scrape_list_page(
            device, item_schema,
            max_items=max_items,
            max_screens=est_screens,
        )

        if not items:
            return self.failed("作品分析页面未采集到数据")

        self.logger.info(f"作品分析采集完成: {len(items)} 条作品")
        return self.success(data={
            "items": items,
            "total_collected": len(items),
        })
