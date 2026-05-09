"""手势相关工具函数（滑动、滚动）"""
import random
import numpy as np
import uiautomator2 as u2


# ============ 基础滑动 ============ #
def curve_swipe(device: u2.Device, start: tuple, end: tuple, duration: float = 0.5):
    """曲线滑动"""
    device.swipe(*start, *end, duration=duration)


def bezier_swipe(d: u2.Device,
                 x0: int, y0: int,
                 x1: int, y1: int,
                 steps: int = 12,
                 duration: int = None) -> None:
    """
    【拟人反检测】使用三次贝塞尔曲线模拟真人滑动
    :param d:        u2 设备实例
    :param x0,y0:    起点
    :param x1,y1:    终点
    :param steps:    曲线采样点数
    :param duration: 滑动总时长（ms），随机默认 180-350
    """
    if duration is None:
        duration = random.randint(180, 350)

    # 控制点加入随机偏移，让每次曲线不同
    cp1 = ((x0 + x1) / 2 + random.randint(-30, 30),
           (y0 + y1) / 2 + random.randint(-20, 20))
    cp2 = ((x0 + x1) / 2 + random.randint(-30, 30),
           (y0 + y1) / 2 + random.randint(-20, 20))

    ts = np.linspace(0, 1, steps)
    points = []
    for t in ts:
        px = (1 - t) ** 3 * x0 + 3 * (1 - t) ** 2 * t * cp1[0] + \
             3 * (1 - t) * t ** 2 * cp2[0] + t ** 3 * x1
        py = (1 - t) ** 3 * y0 + 3 * (1 - t) ** 2 * t * cp1[1] + \
             3 * (1 - t) * t ** 2 * cp2[1] + t ** 3 * y1
        points.append((int(px), int(py)))

    d.swipe_points(points, duration=duration)


def human_swipe(d: u2.Device,
                start_x: int,
                start_y: int,
                end_x: int,
                end_y: int,
                use_bezier: bool = False,
                duration: float = None,
                offset_scale: float = 1.0) -> None:
    """
    【拟人反检测】拟人化滑动包装方法（纯粹添加拟人特性）
    :param d:            u2 设备实例
    :param start_x:      起始X坐标（绝对像素值）
    :param start_y:      起始Y坐标（绝对像素值）
    :param end_x:        结束X坐标（绝对像素值）
    :param end_y:        结束Y坐标（绝对像素值）
    :param use_bezier:   是否使用贝塞尔曲线（更拟人但稍慢）
    :param duration:     滑动时长（秒），None则随机0.3-0.45秒
    :param offset_scale: 偏移缩放系数，0=无偏移，1=正常偏移(±5%或±50px)
    
    拟人特性：
    - 添加随机偏移（可通过offset_scale调整）
    - 随机或指定滑动时长
    - 可选贝塞尔曲线轨迹
    """
    # 导入 human_sleep（避免循环导入）
    from openclaw_agent.engine.utils.interaction import human_sleep
    
    ws, hs = d.window_size()
    
    # 计算滑动距离
    distance = ((end_x - start_x) ** 2 + (end_y - start_y) ** 2) ** 0.5
    max_offset = min(50, int(distance * 0.05)) * offset_scale
    
    # 添加随机偏移（起点和终点都偏移，保持方向一致）
    offset_x = random.randint(-int(max_offset), int(max_offset))
    offset_y = random.randint(-int(max_offset // 2), int(max_offset // 2))
    
    x1 = start_x + offset_x
    y1 = start_y + offset_y
    x2 = end_x + offset_x
    y2 = end_y + offset_y
    
    # 限制在屏幕范围内
    x1 = max(10, min(ws - 10, x1))
    y1 = max(10, min(hs - 10, y1))
    x2 = max(10, min(ws - 10, x2))
    y2 = max(10, min(hs - 10, y2))
    
    # 使用贝塞尔曲线或直线滑动
    if use_bezier:
        # 贝塞尔曲线滑动，时长180-350ms
        bezier_swipe(d, x1, y1, x2, y2)
    else:
        # 普通滑动，使用指定或随机时长（默认0.3-0.45秒，控制惯性）
        if duration is None:
            duration = random.uniform(0.3, 0.45)
        d.swipe(x1, y1, x2, y2, duration=duration)


# ============ 滚动相关 ============ #
def scroll_one_page(device: u2.Device, direction: str = "up"):
    """滚动一屏"""
    width, height = device.window_size()
    start_x = width // 2
    start_y = int(height * 0.8) if direction == "up" else int(height * 0.2)
    end_y = int(height * 0.2) if direction == "up" else int(height * 0.8)
    device.swipe(start_x, start_y, start_x, end_y, duration=0.5)


def scroll_down_70percent(d: u2.Device):
    """【拟人反检测】往下看70%左右，上划（手指从下往上滑，两次34-35%滑动，共68-70%）"""
    from openclaw_agent.engine.utils.interaction import human_sleep
    
    ws, hs = d.window_size()
    
    # 第一次滑动：手指从下往上滑34-35%
    scroll_distance_1 = int(hs * random.uniform(0.34, 0.35))
    start_y = int(hs * 0.50)  # 从50%位置开始
    end_y = start_y - scroll_distance_1  # 手指向上滑
    human_swipe(d, ws // 2, start_y, ws // 2, end_y,
                duration=0.6, offset_scale=0.1)
    human_sleep(0.2, 0.1)
    
    # 第二次滑动：手指从下往上滑34-35%
    scroll_distance_2 = int(hs * random.uniform(0.34, 0.35))
    start_y = int(hs * 0.50)
    end_y = start_y - scroll_distance_2  # 手指向上滑
    human_swipe(d, ws // 2, start_y, ws // 2, end_y,
                duration=0.6, offset_scale=0.1)
    human_sleep(0.5, 0.2)


def scroll_up_70percent(d: u2.Device):
    """【拟人反检测】往上看70%左右，下划（手指从上往下滑，两次34-35%滑动，共68-70%）"""
    from openclaw_agent.engine.utils.interaction import human_sleep
    
    ws, hs = d.window_size()
    
    # 第一次滑动：手指从上往下滑34-35%
    scroll_distance_1 = int(hs * random.uniform(0.34, 0.35))
    start_y = int(hs * 0.50)  # 从50%位置开始
    end_y = start_y + scroll_distance_1  # 手指向下滑
    human_swipe(d, ws // 2, start_y, ws // 2, end_y,
                duration=0.6, offset_scale=0.1)
    human_sleep(0.3, 0.1)
    
    # 第二次滑动：手指从上往下滑34-35%
    scroll_distance_2 = int(hs * random.uniform(0.34, 0.35))
    start_y = int(hs * 0.50)
    end_y = start_y + scroll_distance_2  # 手指向下滑
    human_swipe(d, ws // 2, start_y, ws // 2, end_y,
                duration=0.6, offset_scale=0.1)
    human_sleep(0.5, 0.2)
