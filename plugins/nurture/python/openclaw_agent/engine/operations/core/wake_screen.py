"""唤醒并解锁设备屏幕"""
import subprocess
import time

from openclaw_agent.device.adb.device import find_adb
from openclaw_agent.engine.core.operation import Operation, OperationResult, ExecutionContext


def _is_screen_off(serial: str | None) -> bool:
    """用 ADB dumpsys power 检测屏幕是否息屏（不会产生唤醒副作用）"""
    adb = find_adb()
    prefix = [adb, "-s", serial] if serial else [adb]
    result = subprocess.run(
        prefix + ["shell", "dumpsys", "power"],
        capture_output=True, text=True, timeout=5,
    )
    # mWakefulness=Asleep → 息屏, mWakefulness=Awake → 亮屏
    for line in result.stdout.splitlines():
        if "mWakefulness=" in line:
            return "Asleep" in line
    return False


def _adb_prefix(serial: str | None) -> list[str]:
    adb = find_adb()
    return [adb, "-s", serial] if serial else [adb]


def ensure_screen_on(device, logger=None) -> None:
    """息屏时唤醒并滑动解锁，已亮屏则跳过。可被多个 Operation 复用。"""
    serial = getattr(device, "serial", None)

    if not _is_screen_off(serial):
        if logger:
            logger.info("屏幕已亮起，跳过唤醒")
        return

    if logger:
        logger.info("屏幕息屏，正在唤醒...")

    prefix = _adb_prefix(serial)

    r = subprocess.run(prefix + ["shell", "input", "keyevent", "KEYCODE_WAKEUP"], capture_output=True, text=True)
    if logger:
        logger.info(f"WAKEUP 结果: rc={r.returncode} stderr={r.stderr.strip()!r}")
    time.sleep(0.5)

    try:
        width, height = device.window_size()
    except Exception as e:
        if logger:
            logger.warning(f"window_size() 失败: {e}，使用默认 1080x2400")
        width, height = 1080, 2400

    swipe_cmd = prefix + [
        "shell", "input", "swipe",
        str(width // 2), str(int(height * 0.8)),
        str(width // 2), str(int(height * 0.2)), "300",
    ]
    if logger:
        logger.info(f"执行滑动解锁: {' '.join(swipe_cmd)}")
    r = subprocess.run(swipe_cmd, capture_output=True, text=True)
    if logger:
        logger.info(f"滑动结果: rc={r.returncode} stderr={r.stderr.strip()!r}")
    time.sleep(0.5)

    subprocess.run(prefix + ["shell", "wm", "dismiss-keyguard"], capture_output=True)
    time.sleep(0.3)

    if logger:
        logger.info("屏幕唤醒流程完成")


class WakeScreenOperation(Operation):
    """唤醒息屏设备并滑动解锁（适用于无密码/PIN 的设备）"""

    MAX_STEPS = 3

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        serial = getattr(device, "serial", None)

        if not _is_screen_off(serial):
            self.logger.info("屏幕已亮起，跳过唤醒")
            return self.skipped("屏幕已亮起，无需唤醒")

        ensure_screen_on(device, self.logger)

        if _is_screen_off(serial):
            return self.failed("唤醒屏幕失败，请检查设备连接")

        return self.success(data={"message": "屏幕已唤醒并解锁"})
