"""
简单实用的日志模块

基于loguru的日志系统
"""
import os
import sys
from pathlib import Path
from contextvars import ContextVar

from loguru import logger
from openclaw_agent.engine.common.config import get_config

# 初始化标志
_initialized = False

# 上下文变量存储 task_id
_task_id_var: ContextVar[str] = ContextVar('task_id', default='-')


def _is_windows_exe():
    """检测是否是 Windows 下的打包 exe"""
    return sys.platform == 'win32' and getattr(sys, 'frozen', False)


def _is_docker_env():
    """检测是否在 Docker 容器中运行"""
    return (
        os.path.exists('/.dockerenv') or 
        os.path.exists('/run/.containerenv') or
        os.getenv('KUBERNETES_SERVICE_HOST') is not None
    )


def _format_with_task_id(record):
    """为日志记录添加 task_id"""
    task_id = _task_id_var.get()
    record["extra"]["task_id"] = task_id
    return True


def setup_logger():
    """设置日志配置"""
    global _initialized
    if _initialized:
        return
    
    config = get_config()
    logging_config = config.logging
    is_debug = logging_config.level == "DEBUG"
    is_docker = _is_docker_env()
    is_win_exe = _is_windows_exe()
    
    # 移除默认处理器
    logger.remove()
    
    # 在原格式基础上添加 task_id
    log_format = logging_config.format.replace(" | {message}", " | [{extra[task_id]}] | {message}")
    
    log_targets = []

    # 控制台输出
    logger.add(
        sys.stderr,
        format=log_format,
        level=logging_config.level,
        colorize=not is_docker,
        filter=_format_with_task_id
    )
    log_targets.append("console")
    
    # 文件输出：非调试、非容器环境
    if not is_debug and not is_docker:
        log_file = Path(logging_config.file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_file),
            format=log_format,
            level=logging_config.level,
            rotation="1 day",
            retention="30 days",
            compression="zip",
            filter=_format_with_task_id
        )
        log_targets.append(f"file({log_file})")
    
    # 输出日志配置信息
    env_info = f"win_exe={is_win_exe} docker={is_docker} debug={is_debug}"
    print(f"Log config: {env_info} targets=[{', '.join(log_targets)}]")
    
    _initialized = True


def get_logger(name: str = "main"):
    """获取日志器"""
    setup_logger()
    return logger.bind(name=name)


def set_task_id(task_id: str):
    """
    设置当前上下文的 task_id
    
    Args:
        task_id: 任务ID
    """
    _task_id_var.set(task_id)


def clear_task_id():
    """清除当前上下文的 task_id"""
    _task_id_var.set('-')


def get_task_id() -> str:
    """获取当前上下文的 task_id"""
    return _task_id_var.get()
