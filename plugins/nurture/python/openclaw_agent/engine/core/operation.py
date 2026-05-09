from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
import uiautomator2 as u2

if TYPE_CHECKING:
    from openclaw_agent.device import PhoneAgent


@dataclass
class OperationResult:
    """Operation 执行结果"""
    status: str  # success, failed, skipped
    duration_ms: int = 0
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    should_continue: bool = True  # 是否继续执行后续操作


@dataclass
class ExecutionContext:
    """Operation 执行上下文（运行时资源）"""
    device: u2.Device  # 设备实例
    agent: PhoneAgent  # AI代理实例
    params: Dict[str, Any]  # 运行时参数
    directives: List[Dict[str, Any]] = field(default_factory=list)  # 社区策略指令


class Operation:
    """
    Operation 基类，提供通用操作封装
    
    设计理念（纯 YAML 配置驱动）：
    - Operation 元数据（name, display_name, description, parameters, preconditions）统一在 YAML 配置中定义
    - Python 类只负责实现执行逻辑（execute 方法）
    - 避免配置与代码的冗余，配置即文档
    """
    
    # 子类可定义必传参数列表（用于快速校验）
    REQUIRED_PARAMS: List[str] = []
    
    # 子类可定义最大执行步数（覆盖全局默认值）
    MAX_STEPS: Optional[int] = None
    
    def __init__(self):
        from openclaw_agent.engine.common import get_logger
        self.logger = get_logger(self.__class__.__name__)
    
    def validate(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        """
        验证参数（基于 REQUIRED_PARAMS）
        
        :param params: 运行时参数
        :return: (是否通过, 错误信息)
        
        注意：
        - 子类可通过定义 REQUIRED_PARAMS 声明必传参数
        - 也可重写此方法实现自定义校验逻辑
        """
        for param_name in self.REQUIRED_PARAMS:
            if not params.get(param_name):
                return False, f"缺少必传参数: {param_name}"
        return True, ""
    
    def check_precondition(self, context: ExecutionContext, op_config: Dict[str, Any]) -> Tuple[bool, str]:
        """
        检查前置条件（子类可重写）

        在 execute() 之前由 Executor 自动调用。用于验证当前页面/状态
        是否满足 operation 的执行条件。

        :param context: 执行上下文
        :param op_config: YAML 中该 operation 的完整配置（含 precondition_feed_type 等）
        :return: (是否通过, 错误信息)
        """
        return True, ""

    def check_postcondition(self, context: ExecutionContext, op_config: Dict[str, Any]) -> Tuple[bool, str]:
        """
        检查后置条件（子类可重写）

        在 execute() 成功后由 Executor 自动调用。用于验证操作执行后
        页面/状态是否符合预期（例如是否意外跳转到了其他页面）。

        :param context: 执行上下文
        :param op_config: YAML 中该 operation 的完整配置（含 postcondition_feed_type 等）
        :return: (是否通过, 错误信息)
        """
        return True, ""

    def recover_state(self, context: ExecutionContext) -> bool:
        """
        操作失败后尝试恢复屏幕状态，由 Executor 自动调用。

        子类可覆盖实现平台/业务特定的恢复逻辑（如关闭弹窗、返回主界面等）。
        返回 True 表示恢复成功，Executor 将继续执行后续操作。
        返回 False 表示恢复失败，Executor 将中断执行链。
        """
        return True

    def execute(self, context: ExecutionContext) -> OperationResult:
        """
        执行操作（子类必须实现）

        :param context: 执行上下文，包含 device（设备实例）, agent（AI代理实例）, params（运行时参数）
        :return: OperationResult 执行结果

        推荐使用方式：
        - 元素操作：from openclaw_agent.engine.utils.interaction import wait_element, guarantee_click, human_sleep
        - 手势操作：from openclaw_agent.engine.utils.gesture import human_swipe, scroll_down_70percent
        - 这些工具函数包含拟人反检测特性，更加健壮和安全
        """
        raise NotImplementedError("子类必须实现 execute 方法")
    
    # 结果返回方法
    def success(self, data: dict = None) -> OperationResult:
        """返回成功结果"""
        return OperationResult(status="success", data=data)
    
    def failed(self, error: str, data: dict = None, should_continue: bool = False) -> OperationResult:
        """
        返回失败结果
        
        :param error: 错误信息
        :param data: 额外数据
        :param should_continue: 是否继续执行后续操作（默认False=停止执行）
        """
        self.logger.error(f"Operation 失败: {error}")
        return OperationResult(status="failed", error=error, data=data, should_continue=should_continue)
    
    def skipped(self, reason: str) -> OperationResult:
        """返回跳过结果"""
        self.logger.info(f"Operation 跳过: {reason}")
        return OperationResult(status="skipped", error=reason)
