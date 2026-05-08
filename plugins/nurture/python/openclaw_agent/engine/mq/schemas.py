from dataclasses import dataclass
from typing import List, Dict, Any, Optional


@dataclass
class OperationConfig:
    """Operation 配置"""
    name: str
    params: Dict[str, Any]


@dataclass
class TaskMessage:
    """任务消息"""
    task_id: str
    device_id: str
    operations: List[OperationConfig]
    timestamp: int
    priority: str = "normal"


@dataclass
class OperationResultItem:
    """Operation 执行结果项"""
    name: str
    status: str
    duration_ms: int
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class ResultMessage:
    """结果消息"""
    task_id: str
    device_id: str
    status: str
    operations_result: List[OperationResultItem]
    total_duration_ms: int
    timestamp: int
