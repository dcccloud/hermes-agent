"""
全局常量
"""


class OperationStatus:
    """Operation 执行状态"""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskStatus:
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority:
    """任务优先级"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# 超时配置
DEFAULT_OPERATION_TIMEOUT = 300  # 秒
DEFAULT_DEVICE_CONNECTION_TIMEOUT = 30  # 秒
DEFAULT_ELEMENT_WAIT_TIMEOUT = 10  # 秒

# 重试配置
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1  # 秒
