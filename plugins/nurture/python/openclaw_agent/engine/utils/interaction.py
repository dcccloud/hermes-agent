"""交互相关工具函数（点击、等待）"""
import random
import time
import uiautomator2 as u2


# ============ 时间相关 ============ #

def human_sleep(base: float = 0.8, spread: float = 0.4) -> None:
    """
    【拟人反检测】拟人化 sleep：三角分布，右偏（人一般会略快）
    :param base:    中心值（秒）
    :param spread:  波动幅度（秒）
    """
    z = random.triangular(-1, 1, 0.2)  # 右偏
    t = max(0.2, base + z * spread)
    time.sleep(t)


# ============ 点击相关 ============ #
def guarantee_click(d: u2.Device, selector, max_retry: int = 3, wait_before: float = 0.3) -> bool:
    """
    【拟人反检测】坐标点击，中心随机偏移，保证点到元素，多次重试：
      1. 等待元素稳定
      2. 可见区域中心 + 15%随机偏移
      3. 拟人点击（按压时长随机）
      4. 失败自动重试
    :param d:           uiautomator2 设备实例
    :param selector:    uiautomator2 Selector
    :param max_retry:   最大重试次数
    :param wait_before: 点击前等待时间
    :return:            True=成功点到；False=元素不存在
    """
    for retry in range(max_retry):
        # 元素存在性检查
        if not selector.exists:
            if retry > 0:
                human_sleep(0.5, 0.2)
                continue
            return False
        
        # 等待元素稳定
        if wait_before > 0:
            human_sleep(wait_before, 0.1)
        
        try:
            # 获取元素信息
            info = selector.info
            node = info["bounds"]
            
            ws, hs = d.window_size()
            left   = max(0, node["left"])
            top    = max(0, node["top"])
            right  = min(ws, node["right"])
            bottom = min(hs, node["bottom"])
            
            if right <= left or bottom <= top:
                if retry < max_retry - 1:
                    human_sleep(0.5, 0.2)
                    continue
                return False

            # 计算中心 + 随机偏移
            w = right - left
            h = bottom - top
            
            # 统一使用15%偏移范围，保证点击准确性和拟人性的平衡
            offset_x = random.randint(-int(w * 0.15), int(w * 0.15))
            offset_y = random.randint(-int(h * 0.15), int(h * 0.15))
            
            x = int(left + w / 2 + offset_x)
            y = int(top  + h / 2 + offset_y)
            x = max(left + 5, min(right - 5, x))
            y = max(top + 5, min(bottom - 5, y))

            # 拟人点击（随机按压时长）
            press_duration = random.randint(30, 80)
            d.click(x, y)
            time.sleep(press_duration / 1000.0)
            
            # 点击后短暂等待，让界面响应
            human_sleep(0.3, 0.1)
            return True
            
        except Exception as e:
            if retry < max_retry - 1:
                human_sleep(0.5, 0.2)
                continue
            return False
    
    return False


def safe_element_click(selector, timeout: float = 15.0, stable_time: float = 0.3) -> bool:
    """
    【拟人反检测】安全元素点击：等待元素出现并使用元素原生click方法点击
    
    特点：
    1. 拟人化延迟（切换注意力）
    2. 等待元素出现
    3. 等待UI稳定
    4. 拟人化点击后延迟
    
    :param selector:    uiautomator2 Selector 对象，如 device(text="确定")
    :param timeout:     等待超时时间（秒）
    :param stable_time: 元素稳定等待时间（秒）
    :return:            True=点击成功；False=元素不存在
    """
    # 拟人化：切换注意力/眼睛移动
    human_sleep(0.2, 0.1)
    
    if not wait_element(selector, timeout=timeout, stable_time=stable_time):
        return False
    
    selector.click()
    
    # 拟人化：点击后短暂停顿
    human_sleep(0.3, 0.1)
    return True


# ============ 等待相关 ============ #
def wait_element(selector, timeout: float = 10.0, stable_time: float = 0.5) -> bool:
    """
    智能等待元素出现（使用 uiautomator2 内置 wait 方法）
    
    用法：
        wait_element(d(text="短剧"), timeout=10)
        wait_element(d(textContains="预估收益"), timeout=15, stable_time=0.5)
    
    :param selector:      uiautomator2 Selector对象，如 d(text="短剧")
    :param timeout:       超时时间（秒）
    :param stable_time:   元素出现后的等待时间（秒），用于UI稳定
    :return:              True=出现；False=超时
    
    注意：拟人化延迟应由调用方在外层控制
    """
    try:
        if selector.wait(timeout=timeout):
            if stable_time > 0:
                time.sleep(stable_time)
            return True
        return False
    except Exception:
        return False
