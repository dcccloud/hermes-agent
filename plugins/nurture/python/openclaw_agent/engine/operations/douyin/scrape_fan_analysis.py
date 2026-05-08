"""采集数据中心「粉丝分析」分页数据（含 3 个子分页）

每个子分页用独立 schema 采集。调用者可通过 params 覆盖各子分页 schema。
operation 职责：切到粉丝分析 → 依次切子分页 tab → 逐屏截图提取
"""
from datetime import datetime, timezone
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.operations.douyin.scrape_utils import click_tab, scrape_page

GROWTH_SCHEMA: dict[str, str] = {
    "total_followers": "总粉丝数/当前粉丝数",
    "new_followers": "新增粉丝数/涨粉",
    "lost_followers": "取关粉丝数/掉粉",
    "net_followers": "净增粉丝数",
    "period": "统计周期，如 近7天 / 近30天",
}

PORTRAIT_SCHEMA: dict[str, str] = {
    "male_pct": "男性占比百分比",
    "female_pct": "女性占比百分比",
    "age_distribution": "年龄段分布，如 {'18-23': '35%', '24-30': '28%'}",
    "top_regions": "地域分布 top5，如 {'广东': '15%', '北京': '10%'}",
    "top_cities": "城市分布 top5，如 {'深圳': '8%', '北京': '7%'}",
    "device_brands": "设备品牌分布，如 {'iPhone': '40%', '华为': '20%'}",
}

INTEREST_SCHEMA: dict[str, str] = {
    "interest_tags": "兴趣标签排名，如 {'音乐': '25%', '搞笑': '18%'}",
    "active_hours": "活跃时段描述，如 '20:00-22:00' 或各时段分布",
    "content_preferences": "内容偏好类型分布，如 {'短视频': '80%', '直播': '15%'}",
}


class ScrapeFanAnalysisOperation(DouyinBaseOperation):
    """采集数据中心「粉丝分析」：依次采集 3 个子分页"""

    MAX_STEPS = 30

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent

        growth_schema = context.params.get("growth_schema", GROWTH_SCHEMA)
        portrait_schema = context.params.get("portrait_schema", PORTRAIT_SCHEMA)
        interest_schema = context.params.get("interest_schema", INTEREST_SCHEMA)

        click_tab(device, agent, "粉丝分析")

        result: dict = {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "sub_pages_collected": [],
        }

        # 子分页 1：粉丝分析（增长趋势）
        self.logger.info("采集子分页: 粉丝分析")
        click_tab(device, agent, "粉丝分析")
        result["growth"] = scrape_page(device, growth_schema, max_screens=3)
        result["sub_pages_collected"].append("fan_analysis")

        # 子分页 2：粉丝画像
        self.logger.info("采集子分页: 粉丝画像")
        click_tab(device, agent, "粉丝画像")
        result["demographics"] = scrape_page(device, portrait_schema, max_screens=4)
        result["sub_pages_collected"].append("fan_portrait")

        # 子分页 3：粉丝兴趣
        self.logger.info("采集子分页: 粉丝兴趣")
        click_tab(device, agent, "粉丝兴趣")
        result["interests"] = scrape_page(device, interest_schema, max_screens=4)
        result["sub_pages_collected"].append("fan_interests")

        self.logger.info(f"粉丝分析采集完成: {len(result['sub_pages_collected'])} 个子分页")
        return self.success(data=result)
