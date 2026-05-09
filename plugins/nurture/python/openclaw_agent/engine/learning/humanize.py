"""Region-based humanized interaction primitives.

All coordinate parameters use absolute pixel values.
LLM-generated recipes MUST use these functions instead of raw
device.click / device.swipe to ensure every execution varies naturally.
"""

import random
import time

import uiautomator2 as u2

from openclaw_agent.engine.utils.gesture import bezier_swipe
from openclaw_agent.engine.utils.interaction import human_sleep


def tap_region(
    device: u2.Device,
    region: tuple[int, int, int, int],
    hold_range: tuple[float, float] = (0.03, 0.08),
) -> None:
    """Tap a random point inside *region* ``(x1, y1, x2, y2)`` with
    randomized press duration.
    """
    x1, y1, x2, y2 = region
    x = random.randint(min(x1, x2), max(x1, x2))
    y = random.randint(min(y1, y2), max(y1, y2))

    ws, hs = device.window_size()
    x = max(5, min(ws - 5, x))
    y = max(5, min(hs - 5, y))

    device.click(x, y)
    hold = random.uniform(*hold_range)
    time.sleep(hold)


def swipe_region(
    device: u2.Device,
    start_region: tuple[int, int, int, int],
    end_region: tuple[int, int, int, int],
    duration_range: tuple[float, float] = (0.3, 0.6),
    use_bezier: bool = True,
) -> None:
    """Swipe from a random point in *start_region* to a random point in
    *end_region* using a Bézier curve for natural-looking motion.
    """
    sx = random.randint(min(start_region[0], start_region[2]),
                        max(start_region[0], start_region[2]))
    sy = random.randint(min(start_region[1], start_region[3]),
                        max(start_region[1], start_region[3]))
    ex = random.randint(min(end_region[0], end_region[2]),
                        max(end_region[0], end_region[2]))
    ey = random.randint(min(end_region[1], end_region[3]),
                        max(end_region[1], end_region[3]))

    ws, hs = device.window_size()
    sx = max(10, min(ws - 10, sx))
    sy = max(10, min(hs - 10, sy))
    ex = max(10, min(ws - 10, ex))
    ey = max(10, min(hs - 10, ey))

    if use_bezier:
        dur_ms = random.randint(int(duration_range[0] * 1000),
                                int(duration_range[1] * 1000))
        bezier_swipe(device, sx, sy, ex, ey, duration=dur_ms)
    else:
        dur_s = random.uniform(*duration_range)
        device.swipe(sx, sy, ex, ey, duration=dur_s)


def long_press_region(
    device: u2.Device,
    region: tuple[int, int, int, int],
    duration_range: tuple[float, float] = (0.5, 1.0),
) -> None:
    """Long-press a random point inside *region*."""
    x1, y1, x2, y2 = region
    x = random.randint(min(x1, x2), max(x1, x2))
    y = random.randint(min(y1, y2), max(y1, y2))

    ws, hs = device.window_size()
    x = max(5, min(ws - 5, x))
    y = max(5, min(hs - 5, y))

    dur_s = random.uniform(*duration_range)
    device.long_click(x, y, duration=dur_s)


def wait(base: float = 1.0, spread: float = 0.3) -> None:
    """Humanized wait — delegates to the existing ``human_sleep``."""
    human_sleep(base, spread)


def double_tap_region(
    device: u2.Device,
    region: tuple[int, int, int, int],
) -> None:
    """Double-tap a random point inside *region*."""
    x1, y1, x2, y2 = region
    x = random.randint(min(x1, x2), max(x1, x2))
    y = random.randint(min(y1, y2), max(y1, y2))

    ws, hs = device.window_size()
    x = max(5, min(ws - 5, x))
    y = max(5, min(hs - 5, y))

    device.double_click(x, y)


def back(device: u2.Device) -> None:
    """Press the Android back button."""
    device.press("back")
    human_sleep(0.5, 0.2)


def detect_page(device: u2.Device, app: str = "douyin") -> str | None:
    """Detect the current page using the app's page state graph.

    Returns the page_id (e.g. "data_center") or None if unknown.
    Uses dump_hierarchy() internally — single ADB call, very fast.
    """
    from openclaw_agent.engine.learning.app_graph import get_app_graph
    return get_app_graph(app).detect_page(device)


def find_path(start: str, goal: str, app: str = "douyin") -> list[str] | None:
    """Find the shortest navigation path between two pages.

    Returns list of page_ids from start to goal, or None if unreachable.
    Uses the merged graph (seed + learned).
    """
    from openclaw_agent.engine.learning.app_graph import get_app_graph
    return get_app_graph(app).find_path(start, goal)
