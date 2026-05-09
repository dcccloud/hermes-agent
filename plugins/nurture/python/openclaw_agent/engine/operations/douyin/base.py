"""
抖音 BaseOperation
封装抖音通用操作
"""

import os
import time
import base64
from io import BytesIO
from typing import Any, Dict, Literal, Optional, Tuple
from pydantic import BaseModel
import uiautomator2 as u2
from openclaw_agent.engine.core.operation import ExecutionContext, Operation
from openclaw_agent.engine.utils.interaction import wait_element


class DouyinBaseOperation(Operation):
    """抖音专属基类"""

    APP_PACKAGE = "com.ss.android.ugc.aweme"

    # -- 前置/后置状态检查 -------------------------------------------------
    #
    # 基类从 YAML state_checks 中自动读取并检查 feed_type 和 app_running。
    # 子类重写 check_precondition/check_postcondition 时应先调用 super()，
    # 然后添加业务级检查（如 like_status、follow_status）。

    def check_precondition(
        self, context: ExecutionContext, op_config: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """检查前置条件。

        从 op_config 中读取 state_checks.precondition，自动处理：
        - feed_type: 检测当前 feed 类型
        - app_running: 确保指定 app 在前台
        兼容旧格式 precondition_feed_type。
        子类可重写并先调 super() 再添加业务检查。
        """
        checks = self._get_state_checks(op_config, "precondition")

        # app_running 检查
        required_app = checks.get("app_running")
        if required_app == "douyin":
            if not self.ensure_app_running(context.device):
                return False, "抖音未在前台且启动失败"

        # feed_type 检查
        required_feed = checks.get("feed_type")
        if required_feed:
            actual = self.detect_feed_type(context.device)
            if actual != required_feed:
                msg = (
                    f"前置检查失败: 当前 feed_type={actual}，"
                    f"要求 {required_feed}"
                )
                self.logger.warning(msg)
                return False, msg
            self.logger.info(
                f"前置检查通过: feed_type={actual} (要求 {required_feed})")

        return True, ""

    def check_postcondition(
        self, context: ExecutionContext, op_config: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """检查后置条件。

        从 op_config 中读取 state_checks.postcondition，自动处理：
        - feed_type: 检测操作后 feed 类型
        兼容旧格式 postcondition_feed_type。
        子类可重写并先调 super() 再添加业务检查。
        """
        checks = self._get_state_checks(op_config, "postcondition")

        # feed_type 检查
        required_feed = checks.get("feed_type")
        if required_feed:
            actual = self.detect_feed_type(context.device)
            if actual != required_feed:
                msg = (
                    f"后置检查失败: 当前 feed_type={actual}，"
                    f"要求 {required_feed}（可能意外跳转到了其他页面）"
                )
                self.logger.warning(msg)
                return False, msg
            self.logger.info(
                f"后置检查通过: feed_type={actual} (要求 {required_feed})")

        return True, ""

    def recover_state(self, context: ExecutionContext) -> bool:
        """通用状态恢复：back 直到回到视频流，兜底重启推荐流。"""
        device = context.device

        # 先尝试 back 回到视频流
        for i in range(5):
            ft = self.detect_feed_type(device)
            if ft == "video":
                self.logger.info(f"recover_state: 已恢复到视频流 (back x{i})")
                return True
            device.press("back")
            time.sleep(0.8)

        # back 无法恢复，重启推荐流
        self.logger.info("recover_state: back 无效，尝试重启推荐流...")
        try:
            from openclaw_agent.engine.operations.douyin.enter_recommend_feed import (
                EnterRecommendFeedOperation,
            )
            op = EnterRecommendFeedOperation()
            result = op.execute(context)
            if result.status == "success":
                self.logger.info("recover_state: 重启推荐流成功")
                return True
            self.logger.warning(f"recover_state: 重启推荐流失败 - {result.error}")
            return False
        except Exception as e:
            self.logger.warning(f"recover_state: 重启推荐流异常 - {e}")
            return False

    @staticmethod
    def _get_state_checks(
        op_config: Dict[str, Any], phase: str,
    ) -> Dict[str, str]:
        """从 op_config 提取 state_checks，兼容旧格式。

        优先读取 state_checks.precondition / state_checks.postcondition；
        如果不存在，回退到旧的 precondition_feed_type / postcondition_feed_type。
        """
        state_checks = op_config.get("state_checks") or {}
        checks = state_checks.get(phase)
        if checks is not None:
            return checks if isinstance(checks, dict) else {}

        # 兼容旧格式
        feed_key = f"{phase}_feed_type"
        feed_val = op_config.get(feed_key)
        if feed_val:
            return {"feed_type": feed_val}
        return {}
    
    def ensure_app_running(self, device: u2.Device) -> bool:
        """确保抖音正在运行"""
        current_app = device.app_current()
        current_package = current_app.get("package")
        
        if current_package != self.APP_PACKAGE:
            self.logger.info(f"当前不在抖音({current_package})，正在切换到抖音...")
            try:
                # 先停止当前APP
                if current_package:
                    self.logger.debug(f"停止当前APP: {current_package}")
                    device.app_stop(current_package)
                    time.sleep(0.5)
                
                # 启动抖音
                device.app_start(self.APP_PACKAGE)
                self.logger.info("已启动抖音，等待界面加载...")
                time.sleep(2.0)  # 增加等待时间，确保抖音完全启动
                
                # 二次验证
                verify_app = device.app_current()
                verify_package = verify_app.get("package")
                if verify_package != self.APP_PACKAGE:
                    self.logger.error(f"启动后验证失败，当前包名: {verify_package}")
                    return False
                
                self.logger.info("✓ 抖音启动成功")
                return True
            except Exception as e:
                self.logger.error(f"启动抖音失败: {e}")
                return False
        return True

    def detect_feed_type(self, device: u2.Device, agent=None, target_type: Optional[str] = None) -> str:
        """
        判断当前推荐流页面是直播还是视频或者是广告

        Args:
            device: u2 设备实例
            agent: PhoneAgent 实例（可选，用于进一步判断内容子类型）
            target_type: 目标内容类型（可选）。当提供且不为 "video" 时，会进一步判断
                当前视频是否属于该类型（例如 "drama" 或任意自定义类型字符串）。

        Returns:
            "video": 视频播放页（普通视频/默认类型）
            "drama": 短剧播放页（当 target_type="drama" 且判定命中时）
            其他字符串: 自定义类型（当 target_type 为该字符串且判定命中时）
            "live": 直播页
            "other": 广告页或其他
        """
        t0 = time.time()
        self.logger.info("detect_feed_type: 开始检测...")

        # 1. 先等待评论元素（5秒）
        comment_elem = device(
            className="android.widget.ImageView",
            descriptionMatches="评论[^，]*，按钮"
        )
        has_comment = wait_element(comment_elem, timeout=5, stable_time=0.5)

        if has_comment:
            # 获取元素详细信息用于日志
            try:
                info = comment_elem.info
                desc = info.get("contentDescription", "N/A")
                self.logger.info(
                    f"detect_feed_type: 检测到评论元素 "
                    f"(content-desc='{desc}')，判定为视频播放页 "
                    f"[耗时 {time.time() - t0:.1f}s]")
            except Exception:
                self.logger.info(
                    f"detect_feed_type: 检测到评论元素，判定为视频播放页 "
                    f"[耗时 {time.time() - t0:.1f}s]")

            # 进一步判断内容子类型（仅当明确提供 target_type，且 target_type != "video"）
            if agent is not None and target_type and target_type != "video":
                try:
                    # 兼容原有短剧快速路径：先用视觉规则判定，没命中再走 LLM 自定义判定
                    if target_type == "drama":
                        if self._check_is_drama_by_vision(device, agent):
                            self.logger.info("detect_feed_type: 进一步判定为短剧播放页（vision）")
                            return "drama"

                    if self._check_is_target_content_by_gpt(device, target_type):
                        self.logger.info(
                            f"detect_feed_type: 进一步判定为目标内容类型 '{target_type}'"
                        )
                        return target_type
                except Exception as e:
                    # 子类型判断失败不应影响基础 feed_type 判定
                    self.logger.warning(
                        f"detect_feed_type: 子类型判定失败（忽略，按 video 返回）: {e}"
                    )

            return "video"

        self.logger.info(
            f"detect_feed_type: 未检测到评论元素 "
            f"[耗时 {time.time() - t0:.1f}s]，继续检测直播标识...")

        # 2. 未找到评论元素，等待直播相关元素（10秒）
        live_elem = device(text="点击进入直播间")
        has_live = wait_element(live_elem, timeout=10, stable_time=0.5)

        if has_live:
            self.logger.info(
                f"detect_feed_type: 检测到直播间元素 "
                f"('点击进入直播间')，判定为直播页 "
                f"[总耗时 {time.time() - t0:.1f}s]")
            return "live"

        # 3. 都没找到，判定为广告或其他
        self.logger.info(
            f"detect_feed_type: 未检测到评论或直播元素，"
            f"判定为广告页或其他 [总耗时 {time.time() - t0:.1f}s]")
        return "other"

    def _check_is_target_content_by_gpt(self, device: u2.Device, target_type: str) -> bool:
        """
        使用 drama_check LLM 配置判断当前视频是否属于指定目标类型。

        注意：
        - 这里不尝试做“多分类”，只回答“是否属于 target_type”；
          SwipeToNextVideoOperation 会循环滑动直到命中。
        - target_type 允许传任意字符串（例如 "drama" / "知识科普" / "美食探店"）。
        """
        from openclaw_agent.engine.common.config import get_config

        cfg = get_config().drama_check
        if not cfg.api_key:
            raise RuntimeError("未配置 drama_check.api_key，无法进行内容类型判定")

        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError("未安装 openai 库，无法进行内容类型判定") from e

        # 截图 -> base64
        screenshot = device.screenshot(format="pillow")
        buf = BytesIO()
        screenshot.save(buf, format="JPEG", quality=85)
        img_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        class TargetTypeResult(BaseModel):
            result: Literal["match", "not_match", "unknown"] = "unknown"
            reason: str = ""

        # 让模型把 target_type 当作一个“标签/类型描述”，并基于画面做判定
        prompt = (
            "任务目标：判断当前抖音视频播放页的内容是否属于【目标内容类型】。\n\n"
            f"【目标内容类型】={target_type}\n\n"
            "判定要求：\n"
            "- 如果画面上的明显线索（如字幕/标签/按钮文案/剧情结构/短剧入口等）支持该类型，输出 match。\n"
            "- 如果画面明显不符合该类型，输出 not_match。\n"
            "- 如果画面线索不足或无法确定，输出 unknown。\n\n"
            "【输出格式要求】：\n"
            "- result: match / not_match / unknown\n"
            "- reason: 用中文简要说明判断依据（50字以内）\n"
        )

        client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
        resp = client.beta.chat.completions.parse(
            model=cfg.model_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"},
                        },
                    ],
                }
            ],
            response_format=TargetTypeResult,
            temperature=0.1,
            max_tokens=150,
        )

        parsed = resp.choices[0].message.parsed
        self.logger.info(
            "内容类型判定 | target_type=%s | result=%s | reason=%s",
            target_type,
            parsed.result,
            parsed.reason,
        )
        return parsed.result == "match"
    
    def check_like_status(self, device: u2.Device, agent=None) -> str:
        """
        检查点赞状态（统一入口）
        
        策略：优先使用 u2 采样点颜色检测，失败时降级使用 vision
        
        Args:
            device: u2 设备实例
            agent: PhoneAgent 实例（可选，用于降级）
        
        Returns:
            "liked": 已点赞
            "not_liked": 未点赞
            "unknown": 无法判断
        """
        # 优先使用 u2 采样点检测
        # status = self.check_like_status_by_u2(device)
        # status = self.check_like_status_by_vision(device, agent)
        status = self.check_like_status_by_gpt(device)
        
        # # u2 无法判断时，降级使用 vision（如果提供了 agent）
        # if status == "unknown" and agent is not None:
        #     self.logger.debug("u2 检测点赞状态失败，降级使用 vision")
        #     status = self.check_like_status_by_vision(device, agent)
        
        return status
    
    def check_follow_status(self, device: u2.Device, agent=None) -> str:
        """
        检查关注状态（统一入口）
        
        策略：优先使用 u2 元素检测，失败时降级使用 vision
        
        Args:
            device: u2 设备实例
            agent: PhoneAgent 实例（可选，用于降级）
        
        Returns:
            "can_follow": 可关注（未关注）
            "already_followed": 已关注
            "unknown": 无法判断
        """
        # 优先使用 u2 元素检测
        status = self.check_follow_status_by_u2(device)
        
        # # u2 无法判断时，降级使用 vision（如果提供了 agent）
        # if status == "unknown" and agent is not None:
        #     self.logger.debug("u2 检测关注状态失败，降级使用 vision")
        #     status = self.check_follow_status_by_vision(device, agent)
        
        return status
    
    def check_follow_status_by_u2(self, device: u2.Device) -> str:
        """
        通过 u2 元素属性检查关注状态（快速、准确）
        
        策略：找到关注按钮（Button + content-desc包含"关注"），检查其子元素
        - 有 ImageView 子元素: 未关注（显示加号图标）
        - 无 ImageView 子元素: 已关注（无加号图标）
        
        Args:
            device: u2 设备实例
        
        Returns:
            "can_follow": 可关注（未关注）
            "already_followed": 已关注
            "unknown": 无法判断
        """
        try:
            self.logger.debug("使用 u2 检查关注状态...")
            
            # 查找 Button + content-desc 包含"关注"
            follow_btn = device(
                className="android.widget.Button",
                descriptionMatches=".*关注.*"
            )
            
            if not follow_btn.exists(timeout=2):
                self.logger.warning("找不到关注按钮")
                return "unknown"
            
            # 获取按钮信息（用于调试）
            btn_info = follow_btn.info
            self.logger.debug(f"关注按钮信息: text={btn_info.get('text')}, desc={btn_info.get('contentDescription')}")
            
            # 检查关注按钮下是否有 ImageView 子元素
            # 有 ImageView → 未关注（显示加号图标）
            # 无 ImageView → 已关注（无加号图标）
            child_imageview = follow_btn.child(className="android.widget.ImageView")
            has_child_imageview = child_imageview.exists(timeout=1)
            
            self.logger.debug(f"关注按钮子元素: has_ImageView={has_child_imageview}")
            
            if has_child_imageview:
                self.logger.info("u2 检测: 可关注（Button下有ImageView子元素）")
                return "can_follow"
            else:
                self.logger.info("u2 检测: 已关注（Button下无ImageView子元素）")
                return "already_followed"
        
        except Exception as e:
            self.logger.warning(f"u2 检测关注状态异常: {e}")
            return "unknown"
    
    def check_like_status_by_u2(self, device: u2.Device) -> str:
        """
        通过采样点颜色检测判断点赞状态（快速、准确、无额外依赖）
        
        策略：
        1. 找到评论按钮的 ImageView（content-desc 包含"评论"）
        2. 找到它前面的 ImageView（点赞按钮）
        3. 截取点赞按钮区域的图像
        4. 在横向和纵向采样几个点
        5. 判断红色点的比例
        
        Args:
            device: u2 设备实例
        
        Returns:
            "liked": 已点赞（红色点多）
            "not_liked": 未点赞（白色点多）
            "unknown": 无法判断
        """
        try:
            self.logger.debug("使用采样点颜色检测点赞状态...")
            
            # 步骤1: 找到评论按钮
            comment_btn = device(
                className="android.widget.ImageView",
                descriptionMatches=".*评论.*"
            )
            
            if not comment_btn.exists(timeout=2):
                self.logger.warning("找不到评论按钮")
                return "unknown"
            
            # 步骤2: 找到评论按钮前面的 ImageView（点赞按钮）
            all_imageviews = device(className="android.widget.ImageView")
            imageview_count = all_imageviews.count
            
            like_btn_candidate = None
            
            for i in range(imageview_count):
                try:
                    elem = device(className="android.widget.ImageView", instance=i)
                    if not elem.exists(timeout=0.1):
                        continue
                    
                    # 获取元素信息
                    info = elem.info
                    desc = info.get("contentDescription", "")
                    
                    if "评论" in desc:
                        # 找到评论按钮，前一个 ImageView 就是点赞按钮
                        if i > 0:
                            like_btn_candidate = device(className="android.widget.ImageView", instance=i-1)
                            self.logger.debug(f"找到点赞按钮: 评论按钮的前一个 ImageView (instance={i-1})")
                        break
                
                except Exception as e:
                    continue
            
            if like_btn_candidate is None:
                self.logger.warning("找不到点赞按钮候选")
                return "unknown"
            
            # 步骤3: 获取点赞按钮的坐标区域
            like_info = like_btn_candidate.info
            like_bounds = like_info.get("bounds", {})
            
            left = like_bounds.get("left", 0)
            top = like_bounds.get("top", 0)
            right = like_bounds.get("right", 0)
            bottom = like_bounds.get("bottom", 0)
            
            width = right - left
            height = bottom - top
            
            self.logger.debug(f"点赞按钮区域: ({left}, {top}, {right}, {bottom}), 尺寸: {width}x{height}")
            
            # 步骤4: 截取屏幕并裁剪点赞按钮区域
            screenshot = device.screenshot(format='pillow')
            like_btn_img = screenshot.crop((left, top, right, bottom))
            
            # 步骤5: 定义采样点（横向和纵向各取5个点）
            sample_points = []
            
            # 横向中线取5个点
            mid_y = height // 2
            for i in range(5):
                x = int(width * (0.2 + i * 0.15))  # 20%, 35%, 50%, 65%, 80%
                sample_points.append((x, mid_y))
            
            # 纵向中线取5个点
            mid_x = width // 2
            for i in range(5):
                y = int(height * (0.2 + i * 0.15))  # 20%, 35%, 50%, 65%, 80%
                sample_points.append((mid_x, y))
            
            # 步骤6: 判断每个采样点是否为红色/粉红色
            def is_red_or_pink(rgb):
                """判断 RGB 颜色是否为红色或粉红色"""
                r, g, b = rgb[0], rgb[1], rgb[2]
                
                # 红色特征：R 明显大于 G 和 B
                # 粉红色特征：R > 180, R 大于 G 和 B
                if r > 180 and r > g + 30 and r > b + 30:
                    return True
                
                # 深红色：R > 150, G < 100, B < 100
                if r > 150 and g < 100 and b < 100:
                    return True
                
                return False
            
            red_count = 0
            total_count = len(sample_points)
            
            for x, y in sample_points:
                try:
                    # 确保坐标在图像范围内
                    if 0 <= x < like_btn_img.width and 0 <= y < like_btn_img.height:
                        pixel = like_btn_img.getpixel((x, y))
                        if is_red_or_pink(pixel):
                            red_count += 1
                            self.logger.debug(f"采样点 ({x}, {y}) RGB={pixel[:3]} -> 红色")
                        else:
                            self.logger.debug(f"采样点 ({x}, {y}) RGB={pixel[:3]} -> 非红色")
                except Exception as e:
                    self.logger.debug(f"采样点 ({x}, {y}) 异常: {e}")
                    continue
            
            # 步骤7: 根据红色点比例判断是否已点赞
            red_ratio = red_count / total_count if total_count > 0 else 0
            threshold = 0.5  # 超过 50% 的点是红色，认为已点赞
            
            self.logger.debug(f"红色采样点: {red_count}/{total_count} = {red_ratio:.1%}")
            
            if red_ratio >= threshold:
                self.logger.info(f"采样检测: 已点赞（红色比例 {red_ratio:.1%}）")
                return "liked"
            else:
                self.logger.info(f"采样检测: 未点赞（红色比例 {red_ratio:.1%}）")
                return "not_liked"
        
        except Exception as e:
            self.logger.warning(f"采样检测点赞状态异常: {e}")
            return "unknown"
    
    def check_follow_status_by_vision(self, device: u2.Device, agent) -> str:
        """
        通过视觉识别检查关注状态（可复用）
        
        策略：截图当前页面，让大模型判断关注按钮状态
        
        Args:
            device: u2 设备实例
            agent: PhoneAgent 实例
        
        Returns:
            "can_follow": 可关注（未关注）
            "already_followed": 已关注
            "unknown": 无法判断
        """
        try:
            self.logger.debug("使用视觉识别检查关注状态...")
            
            # 使用 run_with_details 获取 thinking + message
            result = agent.run_with_details(
                "任务目标：判断当前抖音视频的作者是否已被关注。\n\n"
                "【重要】操作要求：\n"
                "1. 必须在视频播放页面完成判断，不要进入作者主页\n"
                "2. 如果进入了作者主页，必须点击返回按钮回到视频播放页\n"
                "3. 只有确认在视频播放页面时，任务才算完成\n\n"
                "判断标准（在视频播放页面）：\n"
                "- 未关注：右侧作者头像下方、点赞按钮上方有红色/白色的 \"+\" 号\n"
                "- 已关注：右侧作者头像下方没有 \"+\" 号（只有头像）\n\n"
                "最终请直接回答：\n"
                "1. 如果未关注（有加号），回复：\"CAN_FOLLOW\"\n"
                "2. 如果已关注（无加号），回复：\"ALREADY_FOLLOWED\"\n"
                "3. 如果无法判断，回复：\"UNKNOWN\"\n"
                "**特别注意**：在最终返回的消息中要包含确定的结果关键词\n"
            )
            
            # 同时检查 thinking 和 message（thinking 中更容易包含关键词）
            full_text = f"{result['thinking']} {result['message']}".upper()
            
            # 精确匹配关键词
            if "CAN_FOLLOW" in full_text:
                self.logger.info(f"视觉识别: 可关注 - {result['message']}")
                return "can_follow"
            elif "ALREADY_FOLLOWED" in full_text:
                self.logger.info(f"视觉识别: 已关注 - {result['message']}")
                return "already_followed"
            elif "UNKNOWN" in full_text:
                self.logger.warning(f"视觉识别: 无法判断 - {result['message']}")
                return "unknown"
            else:
                self.logger.warning(f"视觉识别: 未找到关键词 - {result['message']}")
                return "unknown"
        
        except Exception as e:
            self.logger.warning(f"视觉识别异常: {e}")
            return "unknown"
    
    def check_like_status_by_gpt(self, device: u2.Device) -> str:
        """
        通过 GPT 模型检查点赞状态（快速版本）
        
        策略：截图后直接调用 GPT API 判断点赞图标颜色
        
        Args:
            device: u2 设备实例
        
        Returns:
            "liked": 已点赞（红色）
            "not_liked": 未点赞（白色/灰色）
            "unknown": 无法判断或检测失败
        """
        try:
            # 从配置读取设置
            from openclaw_agent.engine.common.config import get_config
            config = get_config()
            
            api_key = config.drama_check.api_key
            if not api_key:
                self.logger.debug("未配置 API Key，跳过 GPT 检测")
                return "unknown"
            
            self.logger.debug(f"使用 {config.drama_check.model_name} 检查点赞状态...")
            
            # 延迟导入 OpenAI
            try:
                from openai import OpenAI
            except ImportError:
                self.logger.warning("未安装 openai 库，跳过 GPT 检测")
                return "unknown"
            
            # 定义响应 Schema
            class LikeStatusResult(BaseModel):
                """点赞状态检测结果"""
                status: Literal["liked", "not_liked", "unknown"] = "unknown"
                reason: str = ""
            
            # 截图并转换为 base64
            screenshot = device.screenshot(format='pillow')
            buffer = BytesIO()
            screenshot.save(buffer, format='JPEG', quality=85)
            img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            # 调用 GPT API
            client = OpenAI(
                api_key=api_key,
                base_url=config.drama_check.base_url
            )
            
            response = client.beta.chat.completions.parse(
                model=config.drama_check.model_name,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "任务目标：判断当前抖音视频页面右侧的点赞按钮（爱心图标）是否已点赞。\n\n"
                                    "判断标准：\n"
                                    "- 未点赞：爱心图标为白色/灰色/透明\n"
                                    "- 已点赞：爱心图标为红色/粉色（填充状态）\n\n"
                                    "【输出格式要求】：\n"
                                    "- status: 填写 'liked'（已点赞）、'not_liked'（未点赞）或 'unknown'（无法判断）\n"
                                    "- reason: 用中文简要说明判断理由（20字以内）\n"
                                )
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_base64}"
                                }
                            }
                        ]
                    }
                ],
                response_format=LikeStatusResult,
                temperature=0.1,
                max_tokens=100
            )
            
            # 解析结构化结果
            status_result = response.choices[0].message.parsed
            
            self.logger.info(
                f"{config.drama_check.model_name} 点赞状态: {status_result.status} | "
                f"理由: {status_result.reason}"
            )
            
            return status_result.status
        
        except Exception as e:
            self.logger.debug(f"{config.drama_check.model_name if 'config' in locals() else 'GPT'} 检测异常: {e}")
            return "unknown"
    
    def check_like_status_by_vision(self, device: u2.Device, agent) -> str:
        """
        通过视觉识别检查点赞状态（可复用）
        
        策略：截图当前页面，让大模型判断点赞图标是否为红色
        
        Args:
            device: u2 设备实例
            agent: PhoneAgent 实例
        
        Returns:
            "liked": 已点赞（红色）
            "not_liked": 未点赞（白色/灰色）
            "unknown": 无法判断
        """
        try:
            self.logger.debug("使用视觉识别检查点赞状态...")
            
            # 使用 run_with_details 获取 thinking + message
            result = agent.run_with_details(
                "任务目标：判断当前抖音视频页面右侧的点赞按钮（爱心图标）是否已点赞。\n\n"
                "判断标准：\n"
                "- 未点赞：爱心图标为白色/灰色/透明\n"
                "- 已点赞：爱心图标为红色/粉色\n\n"
                "【输出格式要求】你必须严格按照以下格式输出（只输出关键词，不要解释）：\n"
                "- 如果爱心是白色/灰色，说明未点赞，只输出：NOT_LIKED\n"
                "- 如果爱心是红色/粉色，说明已点赞，只输出：LIKED\n"
                "- 如果无法判断，只输出：UNKNOWN\n"
                "**特别注意**：在最终返回的消息中要包含确定的结果关键词\n"
            )
            
            # 同时检查 thinking 和 message（thinking 中更容易包含关键词）
            full_text = f"{result['thinking']} {result['message']}".upper()
            
            # 精确匹配关键词
            if "NOT_LIKED" in full_text or "未点赞" == result['message']:
                self.logger.info(f"视觉识别: 未点赞 - {result['message']}")
                return "not_liked"
            elif "LIKED" in full_text and "NOT_LIKED" not in full_text or "已点赞" == result['message']:
                self.logger.info(f"视觉识别: 已点赞 - {result['message']}")
                return "liked"
            elif "UNKNOWN" in full_text:
                self.logger.warning(f"视觉识别: 无法判断 - {result['message']}")
                return "unknown"
            else:
                self.logger.warning(f"视觉识别: 未找到关键词 - {result['message']}")
                return "unknown"
        
        except Exception as e:
            self.logger.warning(f"视觉识别异常: {e}")
            return "unknown"
    
    def _check_is_drama_by_gpt(self, device: u2.Device) -> bool:
        """
        通过 GPT模型 判断是否是短剧播放页（新方法）
        
        策略：截图后直接调用 GPT API 判断
        
        Args:
            device: u2 设备实例
        
        Returns:
            True: 是短剧
            False: 不是短剧或判断失败
        """
        try:
            # 从配置读取短剧检测设置
            from openclaw_agent.engine.common.config import get_config
            config = get_config()
            
            api_key = config.drama_check.api_key
            if not api_key:
                self.logger.debug("未配置短剧检测 API Key，跳过检测")
                return False
            
            self.logger.debug(f"使用 {config.drama_check.model_name} 判断是否是短剧...")
            
            # 延迟导入 OpenAI（避免未安装时报错）
            try:
                from openai import OpenAI
            except ImportError:
                self.logger.warning("未安装 openai 库，跳过短剧检测")
                return False
            
            # 定义响应 Schema
            class DramaDetectionResult(BaseModel):
                """短剧检测结果"""
                result: Literal["is_drama", "not_drama"] = "not_drama"
                reason: str = ""
            
            # 截图并转换为 base64
            screenshot = device.screenshot(format='pillow')
            buffer = BytesIO()
            screenshot.save(buffer, format='JPEG', quality=85)
            img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            # 调用检测 API
            client = OpenAI(
                api_key=api_key,
                base_url=config.drama_check.base_url
            )
            
            response = client.beta.chat.completions.parse(
                model=config.drama_check.model_name,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "任务目标：判断当前抖音播放页面是否是短剧内容。\n\n"
                                    "短剧特征：\n"
                                    "- 可能界面上下方区域会显示\"短剧\"等字样\n"
                                    "- 底部可能有引导观看完整正片的入口\n\n"
                                    "【输出格式要求】：\n"
                                    "- result: 填写 'is_drama' 或 'not_drama'\n"
                                    "- reason: 用中文简要说明判断理由（50字以内）\n"
                                )
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_base64}"
                                }
                            }
                        ]
                    }
                ],
                response_format=DramaDetectionResult,
                temperature=0.1,
                max_tokens=200
            )
            
            # 解析结构化结果
            detection_result = response.choices[0].message.parsed
            
            self.logger.info(
                f"{config.drama_check.model_name} 判断结果: {detection_result.result} | "
                f"理由: {detection_result.reason}"
            )
            
            if detection_result.result == "is_drama":
                self.logger.info(f"✓ {config.drama_check.model_name} 判定为短剧")
                return True
            else:
                self.logger.info(f"✗ {config.drama_check.model_name} 判定为非短剧")
                return False
        
        except Exception as e:
            error_msg = f"短剧检测模型调用失败: {str(e)}"
            self.logger.error(error_msg)
            raise RuntimeError(error_msg) from e
    
    def _check_is_drama_by_vision(self, device: u2.Device, agent) -> bool:
        """
        通过视觉识别判断是否是短剧播放页（私有方法）
        
        策略：截图当前页面，让大模型判断是否是短剧内容
        
        Args:
            device: u2 设备实例
            agent: PhoneAgent 实例
        
        Returns:
            True: 是短剧
            False: 不是短剧
        """
        return self._check_is_drama_by_gpt(device)
        
        
        try:
            self.logger.debug("使用视觉识别判断是否是短剧...")
            
            # 使用 run_with_details 获取 thinking + message
            result = agent.run_with_details(
                "任务目标：判断当前抖音播放页面是否是短剧内容。\n\n"
                "短剧特征：\n"
                "- 可能显示\"短剧\"等字样\n"
                "- 底部可能有引导观看完整正片的入口\n"
                "【输出格式要求】你必须严格按照以下格式输出（只输出关键词，不要解释）：\n"
                "- 如果是短剧内容，只输出：IS_DRAMA\n"
                "- 如果不是短剧（普通短视频），只输出：NOT_DRAMA\n"
                "**特别注意**：在最终返回的消息中要包含确定的结果关键词\n"
            )
            
            # 同时检查 thinking 和 message（thinking 中更容易包含关键词）
            full_text = f"{result['thinking']} {result['message']}".upper()
            
            # 精确匹配关键词
            if "IS_DRAMA" in full_text and "NOT_DRAMA" not in full_text:
                self.logger.info(f"视觉识别: 是短剧 - {result['message']}")
                return True
            elif "NOT_DRAMA" in full_text:
                self.logger.info(f"视觉识别: 不是短剧 - {result['message']}")
                return False
            else:
                # 默认认为不是短剧
                self.logger.warning(f"视觉识别: 未找到关键词，默认判定为非短剧 - {result['message']}")
                return False
        
        except Exception as e:
            self.logger.warning(f"视觉识别异常: {e}，默认判定为非短剧")
            return False