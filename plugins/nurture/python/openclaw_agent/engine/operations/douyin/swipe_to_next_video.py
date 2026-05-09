"""向上滑动到下一个视频"""
import time
import random
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.gesture import human_swipe


class SwipeToNextVideoOperation(DouyinBaseOperation):
    """
    向上滑动到下一个视频（支持连续滑动多次 或 查找特定类型内容）
    
    参数:
        count (int): 滑动次数，默认 1（当 target_content_type=None 时生效）
        watch_duration (float, optional): 每个视频观看时长（秒），默认 None（不观看）
            - 如果设置，会在每次滑动后观看指定时长（±20% 随机波动）
            - 用于模拟真人浏览行为
        target_content_type (str, optional): 目标内容类型，默认 None（不查找）
            - None: 按 count 次数滑动（原有逻辑）
            - "video": 不断滑动直到找到普通视频作品
            - "drama": 不断滑动直到找到短剧视频
            - 其他任意字符串: 自定义类型（例如 "知识科普" / "美食探店" / "动漫"），
              会把该字符串传给大模型进行内容类型判定
        target_action (str, optional): 目标动作，默认 None（不检查）
            - None: 不检查能否执行动作
            - "like": 查找可点赞的内容（未点赞）
            - "follow": 查找可关注的内容（未关注）
        max_swipes (int, optional): 最多滑动次数，默认 15（仅当 target_content_type 设置时生效）
    """
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        params = context.params
        
        count = params.get("count", 1)  # 默认滑动1次
        watch_duration = params.get("watch_duration", 5)  # 每个视频观看时长（秒）
        target_content_type = params.get("target_content_type", None)  # 目标内容类型
        target_action = params.get("target_action", None)  # 目标动作
        max_swipes = params.get("max_swipes", 8)  # 最多滑动次数（查找模式）
        
        # 确保在抖音
        if not self.ensure_app_running(device):
            return self.failed("抖音启动失败")
        
        # 参数验证：target_content_type 允许 None 或任意字符串（自定义类型）
        if target_content_type is not None and not isinstance(target_content_type, str):
            return self.failed(
                f"不支持的 target_content_type 类型: {type(target_content_type)}，"
                f"必须为字符串或不传（按次数滑动）"
            )

        # 参数验证：target_action 只能是 None, "like", "follow"
        if target_action is not None and target_action not in ["like", "follow"]:
            return self.failed(
                f"不支持的 target_action: '{target_action}'，"
                f"必须是 'like'（可点赞）或 'follow'（可关注）或不传"
            )

        # 简单模式：按 count 次数滑动
        if target_content_type is None:
            return self._execute_count_mode(device, count, watch_duration)

        # 查找模式：忽略 count，使用 max_swipes
        return self._execute_find_mode(device, agent, target_content_type, target_action, max_swipes, watch_duration)
    
    def _execute_count_mode(self, device, count: int, watch_duration) -> OperationResult:
        """
        按次数滑动模式（简单刷视频）

        自动跳过直播间和广告：每次滑动后检测 feed_type，
        如果不是普通视频则自动再滑（最多重试 3 次）。

        :param device: u2 设备实例
        :param count: 滑动次数
        :param watch_duration: 每个视频观看时长（秒）
        :return: OperationResult
        """
        max_skip_retries = 3
        self.logger.info(f"开始滑动 {count} 次（观看 {watch_duration}s/个）")
        for i in range(count):
            self.logger.info(f"第 {i+1}/{count} 次滑动...")
            self._swipe_to_next_with_watch(device, watch_duration)

            # 自动跳过非视频内容（直播间、广告等）
            for retry in range(max_skip_retries):
                ft = self.detect_feed_type(device)
                if ft == "video":
                    break
                self.logger.info(
                    f"落在 {ft}（非视频），自动跳过 ({retry+1}/{max_skip_retries})...")
                self._swipe_to_next_with_watch(device, None)  # 快速滑过，不等观看

        return self.success({
            "message": f"已滑动 {count} 次",
            "swipe_count": count,
        })

    def _execute_find_mode(self, device, agent, target_type: str, target_action: str, max_swipes: int, watch_duration) -> OperationResult:
        """
        查找目标类型内容模式（不断滑动直到找到目标类型和可执行动作）
        
        :param device: u2 设备实例
        :param agent: PhoneAgent 实例
        :param target_type: 目标内容类型（"video" 或 "drama"）
        :param target_action: 目标动作（"like", "follow", None）
        :param max_swipes: 最多滑动次数
        :param watch_duration: 观看时长（可选）
        :return: OperationResult
        """
        action_desc = f" + {target_action}" if target_action else ""
        self.logger.info(f"开始查找目标: {target_type}{action_desc}（最多滑动 {max_swipes} 次）")
        
        for i in range(max_swipes):
            # 先滑动到下一个内容
            self.logger.info(f"第 {i+1}/{max_swipes} 次滑动...")
            self._swipe_to_next_with_watch(device, watch_duration)
            
            # 检测新页面类型
            try:
                current_type = self.detect_feed_type(device, agent, target_type)
                self.logger.info(f"当前页面类型: {current_type}")
            except RuntimeError as e:
                # 检测失败，记录错误并返回失败
                self.logger.error(f"页面类型检测失败: {e}")
                return self.failed(str(e))
            
            # 检查是否匹配目标类型（video 或 drama）
            if current_type == target_type:
                # 如果指定了 target_action，还需检查能否执行该动作
                if target_action:
                    action_ok = self._check_action_status(device, agent, target_action)
                    if action_ok:
                        self.logger.info(f"✓ 找到目标: {target_type} + 可{target_action}（滑动 {i+1} 次后）")
                        return self.success({
                            "message": f"找到目标内容: {target_type} + 可{target_action}",
                            "swipe_count": i + 1,
                            "target_type": target_type,
                            "target_action": target_action
                        })
                    else:
                        self.logger.info(f"内容类型匹配但不可{target_action}，继续滑动...")
                else:
                    # 不检查动作，直接返回
                    self.logger.info(f"✓ 找到目标内容类型: {target_type}（滑动 {i+1} 次后）")
                    return self.success({
                        "message": f"找到目标内容类型: {target_type}",
                        "swipe_count": i + 1,
                        "target_type": target_type
                    })
            else:
                self.logger.info(f"当前内容类型为 {current_type}（非 {target_type}），继续滑动...")
        
        # 达到最大滑动次数仍未找到
        return self.failed(f"滑动 {max_swipes} 次后仍未找到目标: {target_type}{action_desc}")

    def _check_action_status(self, device, agent, action: str) -> bool:
        """
        检查当前内容是否可执行指定动作
        
        :param device: u2 设备实例
        :param agent: PhoneAgent 实例
        :param action: 动作类型（"like"、"follow" 或 "drama"）
        :return: True 表示可执行，False 表示不可执行
        """
        if action == "like":
            like_status = self.check_like_status(device, agent)
            return like_status == "not_liked"
        elif action == "follow":
            follow_status = self.check_follow_status(device, agent)
            return follow_status == "can_follow"
        else:
            return True
    
    def _swipe_to_next_with_watch(self, device, watch_duration):
        """
        滑动到下一个内容（带观看时长，拟人化）
        
        :param device: u2 设备实例
        :param watch_duration: 观看时长（秒），None 表示快速浏览
        """
        # 观看当前内容（如果设置了 watch_duration）
        if watch_duration is not None:
            actual_watch_time = watch_duration * random.uniform(0.8, 1.2)
            actual_watch_time = max(3, min(90, actual_watch_time))
            self.logger.info(f"观看当前内容 {actual_watch_time:.1f} 秒...")
            time.sleep(actual_watch_time)
        else:
            # 快速浏览（减少等待时间以提高速度）
            quick_view = random.uniform(0.2, 0.5)
            time.sleep(quick_view)
        
        # 拟人化滑动
        width, height = device.window_size()
        
        # 起始点：屏幕中央左右10-15%范围内随机
        start_x_offset = random.uniform(-0.15, 0.15)
        start_x = int(width * (0.5 + start_x_offset))
        
        # 起始Y：底部75-85%之间随机
        start_y_ratio = random.uniform(0.75, 0.85)
        start_y = int(height * start_y_ratio)
        
        # 终点X：添加横向漂移（-8%到+8%）
        end_x_drift = random.uniform(-0.08, 0.08)
        end_x = int(start_x + width * end_x_drift)
        
        # 终点Y：顶部15-25%之间随机
        end_y_ratio = random.uniform(0.15, 0.25)
        end_y = int(height * end_y_ratio)
        
        # 限制在屏幕有效范围内
        start_x = max(int(width * 0.1), min(int(width * 0.9), start_x))
        end_x = max(int(width * 0.1), min(int(width * 0.9), end_x))
        
        self.logger.debug(
            f"拟人化滑动: ({start_x}, {start_y}) -> ({end_x}, {end_y}) "
            f"[横向漂移: {end_x - start_x}px]"
        )

         # 使用 human_swipe
        human_swipe(device, start_x, start_y, end_x, end_y, duration=random.uniform(0.1, 0.2))
        
        # 等待内容加载（减少等待时间以提高速度）
        load_time = random.uniform(0.5, 1.0)
        time.sleep(load_time)