"""设备相关工具函数"""
import uiautomator2 as u2


def optimize_device(d: u2.Device):
    """优化设备设置"""
    try:
        # 关闭动画
        d.shell("settings put global window_animation_scale 0")
        d.shell("settings put global transition_animation_scale 0")
        d.shell("settings put global animator_duration_scale 0")
        
        # 保持屏幕常亮
        d.shell("settings put system screen_off_timeout 2147483647")
        d.screen_on()
        
        # 优化 uiautomator2 相关应用
        packages = [
            "com.github.uiautomator",
            "com.github.uiautomator.test",
        ]
        for pkg in packages:
            # 加入省电白名单
            d.shell(f"dumpsys deviceidle whitelist +{pkg}")
            # 设置为活跃状态
            d.shell(f"am set-standby-bucket {pkg} active")
    except:
        pass


def is_keyboard_open(d: u2.Device) -> bool:
    """
    通过 ADB 检测软键盘是否弹出（高精度，跨 Android 版本）
    
    :param d: uiautomator2 设备实例
    :return: True=键盘打开, False=键盘关闭
    """
    try:
        output = d.shell("dumpsys input_method").output
        return "mInputShown=true" in output
    except Exception:
        # 检测失败，假设键盘未打开
        return False


def preflight(d: u2.Device):
    """设备启动前检查"""
    from openclaw_agent.engine.utils.interaction import human_sleep
    
    try:
        # 确保屏幕唤醒
        if not d.info['screenOn']:
            d.screen_on()
            human_sleep(1)
    except:
        pass
