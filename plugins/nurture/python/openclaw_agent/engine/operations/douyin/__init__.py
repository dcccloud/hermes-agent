from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.operations.douyin.enter_recommend_feed import EnterRecommendFeedOperation
from openclaw_agent.engine.operations.douyin.swipe_to_next_video import SwipeToNextVideoOperation
from openclaw_agent.engine.operations.douyin.watch_video import WatchVideoOperation
from openclaw_agent.engine.operations.douyin.give_a_like import DouyinGiveALikeOperation
from openclaw_agent.engine.operations.douyin.post_comment import PostCommentOperation
from openclaw_agent.engine.operations.douyin.enter_author_profile import EnterAuthorProfileOperation
from openclaw_agent.engine.operations.douyin.follow_author import FollowAuthorOperation
from openclaw_agent.engine.operations.douyin.exit_author_profile import ExitAuthorProfileOperation
from openclaw_agent.engine.operations.douyin.share_to_wechat import ShareToWechatOperation
from openclaw_agent.engine.operations.douyin.trigger_search import TriggerSearchOperation
from openclaw_agent.engine.operations.douyin.select_search_result import SelectSearchResultOperation
from openclaw_agent.engine.operations.douyin.view_my_profile import ViewMyProfileOperation
from openclaw_agent.engine.operations.douyin.enter_data_center import EnterDataCenterOperation
from openclaw_agent.engine.operations.douyin.scrape_overview import ScrapeOverviewOperation
from openclaw_agent.engine.operations.douyin.scrape_content_analysis import ScrapeContentAnalysisOperation
from openclaw_agent.engine.operations.douyin.scrape_fan_analysis import ScrapeFanAnalysisOperation

__all__ = [
    "DouyinBaseOperation",
    "EnterRecommendFeedOperation",
    "SwipeToNextVideoOperation",
    "WatchVideoOperation",
    "DouyinGiveALikeOperation",
    "PostCommentOperation",
    "EnterAuthorProfileOperation",
    "FollowAuthorOperation",
    "ExitAuthorProfileOperation",
    "ShareToWechatOperation",
    "TriggerSearchOperation",
    "SelectSearchResultOperation",
    "ViewMyProfileOperation",
    "EnterDataCenterOperation",
    "ScrapeOverviewOperation",
    "ScrapeContentAnalysisOperation",
    "ScrapeFanAnalysisOperation",
]
