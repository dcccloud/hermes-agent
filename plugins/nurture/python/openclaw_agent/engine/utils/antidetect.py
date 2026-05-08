"""【拟人反检测】模拟人类行为相关工具函数"""
import random
import uiautomator2 as u2


def simulate_human_browsing(d: u2.Device, logger=None):
    """
    【拟人反检测】模拟人类随机浏览行为（保证页面位置不变）
    :param d: 设备实例
    :param logger: 可选的日志记录器
    """
    from openclaw_agent.engine.utils.interaction import human_sleep
    from openclaw_agent.engine.utils.gesture import human_swipe
    
    if logger:
        logger.info("→ 执行反检测：模拟人类随机浏览行为...")
    
    ws, hs = d.window_size()
    
    # 随机选择行为类型
    action_type = random.choice(['sleep', 'swipe_pair'])
    
    if action_type == 'sleep':
        human_sleep(random.uniform(1.5, 3.0))
        if logger:
            logger.info("✓ 反检测行为完成（停顿观察）")
    else:
        # 成对滑动，确保页面位置不变
        start_y = int(hs * 0.5)
        offset = int(hs * random.uniform(0.08, 0.12))
        
        # 先向下滑动
        human_swipe(d, ws//2, start_y - offset, ws//2, start_y + offset, use_bezier=False)
        human_sleep(random.uniform(0.5, 1.0))
        
        # 再向上滑动相同距离，回到原位
        human_swipe(d, ws//2, start_y + offset, ws//2, start_y - offset, use_bezier=False)
        human_sleep(random.uniform(0.3, 0.8))
        
        if logger:
            logger.info("✓ 反检测行为完成（往返滑动）")


def ensure_element_at_initial_position(d: u2.Device, element_selector, initial_y: float, logger=None):
    """
    【拟人反检测】确保参考元素回到初始Y坐标位置（避免因页面滚动导致位置偏移）
    :param d: 设备实例
    :param element_selector: 参考元素的u2选择器
    :param initial_y: 参考元素的初始Y坐标
    :param logger: 可选的日志记录器
    :return: 当前元素的Y坐标（如果元素不存在返回initial_y）
    """
    from openclaw_agent.engine.utils.interaction import human_sleep
    from openclaw_agent.engine.utils.gesture import human_swipe
    
    try:
        if not element_selector.exists:
            if logger:
                logger.warning("参考元素不存在，跳过位置检查")
            return initial_y
        
        # 获取当前Y坐标
        bounds = element_selector.info.get('visibleBounds') or element_selector.info.get('bounds', {})
        current_y = bounds.get('top', initial_y)
        
        if logger:
            logger.info(f"参考元素Y坐标: 初始={initial_y}, 当前={current_y}")
        
        # 如果当前Y坐标低于初始值，说明页面向下滚动了，需要向下滑动补偿
        if current_y < initial_y:
            offset = initial_y - current_y
            ws, hs = d.window_size()
            
            if logger:
                logger.info(f"→ 页面下滚了{offset}px，向下滑动补偿...")
            
            # 手指向下滑动（从上往下），让页面内容向下移动
            # 滑动距离略大于offset，确保能完全补偿
            swipe_distance = min(int(offset * 1.2), int(hs * 0.4))  # 最多滑动40%屏幕高度
            start_y = int(hs * 0.3)
            end_y = start_y + swipe_distance
            
            human_swipe(d, ws//2, start_y, ws//2, end_y, use_bezier=False)
            human_sleep(0.5, 0.2)
            
            # 获取补偿后的Y坐标
            if element_selector.exists:
                bounds = element_selector.info.get('visibleBounds') or element_selector.info.get('bounds', {})
                new_y = bounds.get('top', current_y)
                if logger:
                    logger.info(f"✓ 补偿后Y坐标: {new_y}")
                return new_y
        else:
            if logger:
                logger.info("✓ 元素位置正常，无需调整")
        
        return current_y
        
    except Exception as e:
        if logger:
            logger.warning(f"检查元素位置出错: {e}")
        return initial_y
