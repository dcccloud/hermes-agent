"""XCTest utilities for iOS device interaction via WebDriverAgent/XCUITest."""

from openclaw_agent.device.xctest.connection import (
    ConnectionType,
    DeviceInfo,
    XCTestConnection,
    list_devices,
    quick_connect,
)
from openclaw_agent.device.xctest.device import (
    back,
    double_tap,
    get_current_app,
    home,
    launch_app,
    long_press,
    swipe,
    tap,
)
from openclaw_agent.device.xctest.input import (
    clear_text,
    type_text,
)
from openclaw_agent.device.xctest.screenshot import get_screenshot

__all__ = [
    # Screenshot
    "get_screenshot",
    # Input
    "type_text",
    "clear_text",
    # Device control
    "get_current_app",
    "tap",
    "swipe",
    "back",
    "home",
    "double_tap",
    "long_press",
    "launch_app",
    # Connection management
    "XCTestConnection",
    "DeviceInfo",
    "ConnectionType",
    "quick_connect",
    "list_devices",
]
