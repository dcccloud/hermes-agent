import threading
from typing import Dict, List
import uiautomator2 as u2


class DeviceManager:
    """u2.Device 连接池管理器"""
    
    def __init__(self):
        self._devices: Dict[str, u2.Device] = {}
        self._lock = threading.Lock()
    
    def get(self, device_id: str) -> u2.Device:
        """获取设备实例，惰性连接"""
        with self._lock:
            if device_id not in self._devices:
                self._devices[device_id] = u2.connect(device_id)
            return self._devices[device_id]
    
    def list_devices(self) -> List[str]:
        """列出已连接设备"""
        with self._lock:
            return list(self._devices.keys())
    
    def disconnect(self, device_id: str):
        """断开设备连接"""
        with self._lock:
            if device_id in self._devices:
                del self._devices[device_id]


device_manager = DeviceManager()
