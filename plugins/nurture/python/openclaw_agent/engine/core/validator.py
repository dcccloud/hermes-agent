from typing import List, Dict, Any
import uiautomator2 as u2


class Validator:
    """条件验证器"""
    
    def validate_preconditions(self, preconditions: List[Dict[str, Any]], device: u2.Device) -> bool:
        """验证准入条件"""
        for condition in preconditions:
            condition_type = condition["type"]
            value = condition["value"]
            
            if condition_type == "app_running":
                if not self._check_app_running(device, value):
                    return False
            elif condition_type == "element_exists":
                if not self._check_element_exists(device, value):
                    return False
        
        return True
    
    def _check_app_running(self, device: u2.Device, package_name: str) -> bool:
        """检查应用是否在运行"""
        current_app = device.app_current()
        return current_app.get("package") == package_name
    
    def _check_element_exists(self, device: u2.Device, selector: Dict[str, Any]) -> bool:
        """检查元素是否存在"""
        return device(**selector).exists
