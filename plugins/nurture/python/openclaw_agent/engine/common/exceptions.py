"""
全局自定义异常
"""


class OperationException(Exception):
    """Operation 执行异常基类"""
    pass


class DeviceConnectionError(OperationException):
    """设备连接异常"""
    pass


class ValidationError(OperationException):
    """参数验证异常"""
    pass


class TimeoutError(OperationException):
    """超时异常"""
    pass


class PreconditionNotMetError(OperationException):
    """准入条件不满足异常"""
    pass


class OperationNotFoundError(OperationException):
    """Operation 未找到异常"""
    pass


class TaskExecutionError(Exception):
    """任务执行异常"""
    pass
