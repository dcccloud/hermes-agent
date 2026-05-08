"""采集数据中心「总览」分页数据

调用者可通过 params.schema 覆盖默认 schema。
operation 职责：切到总览 tab → 逐屏截图 + vision 按 schema 提取 → 滚到底返回
"""
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.operations.douyin.scrape_utils import click_tab, scrape_page, parse_count

DEFAULT_SCHEMA: dict[str, str] = {
    "period": "统计周期，如 近7天 / 近30天",
    "plays": "播放量/播放次数",
    "likes": "点赞数",
    "comments": "评论数",
    "shares": "分享数/转发数",
    "new_followers": "新增粉丝数",
    "lost_followers": "取关/掉粉数",
    "net_followers": "净增粉丝数",
    "profile_views": "主页访问量/主页浏览量",
    "favorites": "收藏数",
}


class ScrapeOverviewOperation(DouyinBaseOperation):
    """采集数据中心「总览」：schema-driven 逐屏截图提取"""

    MAX_STEPS = 20

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        schema = context.params.get("schema", DEFAULT_SCHEMA)
        max_screens = context.params.get("max_screens", 4)

        click_tab(device, agent, "总览")

        raw = scrape_page(device, schema, max_screens=max_screens)

        if all(v is None for v in raw.values()):
            return self.failed("总览页面未采集到数据")

        # 数字字段转整数，保留 None（未采到）和 0（实际值）的区分
        for k, v in raw.items():
            if k == "period" or v is None:
                continue
            raw[k] = parse_count(v) if isinstance(v, str) else v

        filled = sum(1 for v in raw.values() if v is not None)
        self.logger.info(f"总览采集完成: {filled}/{len(schema)} 字段")
        return self.success(data=raw)
