"""
配置管理模块

支持多环境配置，从YAML文件加载配置。
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    """应用配置"""
    name: str = Field(default="operation-engine")
    version: str = Field(default="1.0.0")
    environment: str = Field(default="dev")
    debug: bool = Field(default=True)


class LoggingConfig(BaseModel):
    """日志配置"""
    level: str = Field(default="DEBUG")
    format: str = Field(default="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} | {message}")
    file: str = Field(default="logs/operation_engine.log")


class MQConfig(BaseModel):
    """消息队列配置"""
    type: str = Field(default="rabbitmq")
    host: str = Field(default="localhost")
    port: int = Field(default=5672)
    username: str = Field(default="guest")
    password: str = Field(default="guest")
    virtual_host: str = Field(default="/")
    
    # 队列配置
    queue: str = Field(default="operation_tasks")
    result_queue: str = Field(default="operation_results")
    actions_response_queue: Optional[str] = Field(default=None)  # 每个 op 结果的队列
    observations_queue: Optional[str] = Field(default=None)  # 整个任务结束后的观察消息队列
    
    # 连接配置
    heartbeat: int = Field(default=60)
    connection_attempts: int = Field(default=3)
    retry_delay: float = Field(default=2.0)
    socket_timeout: int = Field(default=15)
    blocked_connection_timeout: int = Field(default=300)
    
    # 任务配置
    task_timeout: int = Field(default=1800)
    prefetch_count: int = Field(default=1)


class DBConfig(BaseModel):
    """数据库配置"""
    type: str = Field(default="mysql")
    host: str = Field(default="localhost")
    port: int = Field(default=3306)
    database: str = Field(default="operation_engine")
    username: str = Field(default="root")
    password: str = Field(default="root")


class AutoGLMConfig(BaseModel):
    """AutoGLM PhoneAgent 配置"""
    base_url: str = Field(default="http://localhost:8000/v1")
    api_key: str = Field(default="EMPTY")
    model_name: str = Field(default="autoglm-phone-9b")
    max_steps: int = Field(default=50, description="operation默认最大执行步数")
    temperature: float = Field(default=0.0, description="模型温度参数，控制输出随机性")


class DramaCheckConfig(BaseModel):
    """短剧检测配置"""
    model_name: str = Field(default="gpt-4o")
    api_key: str = Field(default="")
    base_url: str = Field(default="https://api.openai.com/v1")


class VisionReadConfig(BaseModel):
    """Vision 读屏模型配置（截屏数据提取）"""
    model_name: str = Field(default="doubao-seed-2-0-pro-260215")
    api_key: str = Field(default="ark-62e34a96-f0a0-4759-a955-49525101f53e-916e5")
    base_url: str = Field(default="https://ark.cn-beijing.volces.com/api/v3")


class RecipeGenConfig(BaseModel):
    """Recipe 生成 LLM 配置（VLM trace → Python recipe）"""
    model_name: str = Field(default="")
    api_key: str = Field(default="")
    base_url: str = Field(default="")


class DevicesConfig(BaseModel):
    """设备池配置"""
    connection_timeout: int = Field(default=30)
    max_retries: int = Field(default=3)


class ExecutorConfig(BaseModel):
    """执行器配置"""
    workers: int = Field(default=2)
    operation_timeout: int = Field(default=300)


class ResumeConfig(BaseModel):
    """可续跑执行配置 — 当任务超时/中断后下次同任务复用 checkpoint 与 VLM 历史"""

    enabled: bool = Field(
        default=True,
        description="启用 op 级 checkpoint 与续跑判定"
    )
    fingerprint_ttl_seconds: int = Field(
        default=1800,
        description="同 device_id+operations 指纹在该窗口内复用 task_id"
    )
    op_signature_mismatch_policy: str = Field(
        default="warn_and_restart",
        description="checkpoint 中 op_name/params 与新请求不一致时的策略：warn_and_restart / strict"
    )
    prior_context_max_actions: int = Field(
        default=30,
        description="prior VLM 历史注入新任务时保留的最大动作数（截断保留首步+最近）"
    )
    in_flight_trace_enabled: bool = Field(
        default=True,
        description="VLM 单步 trace 增量落盘（Phase 2 能力，超时切断时也不丢探索成果）"
    )


class Config(BaseModel):
    """主配置类"""
    app: AppConfig = Field(default_factory=AppConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    mq: MQConfig = Field(default_factory=MQConfig)
    db: DBConfig = Field(default_factory=DBConfig)
    autoglm: AutoGLMConfig = Field(default_factory=AutoGLMConfig)
    drama_check: DramaCheckConfig = Field(default_factory=DramaCheckConfig)
    vision_read: VisionReadConfig = Field(default_factory=VisionReadConfig)
    recipe_gen: RecipeGenConfig = Field(default_factory=RecipeGenConfig)
    devices: DevicesConfig = Field(default_factory=DevicesConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    resume: ResumeConfig = Field(default_factory=ResumeConfig)

    @classmethod
    def from_yaml(cls, config_path: str) -> "Config":
        """从YAML文件加载配置"""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        
        return cls(**config_data)

    @classmethod
    def from_environment(cls) -> "Config":
        """根据APP_ENV环境变量选择配置文件加载"""
        env = os.getenv("APP_ENV", "dev")
        
        config_dir = Path(__file__).parent.parent.parent / "config"
        config_path = config_dir / f"{env}.yaml"
        
        print(f"加载配置 | APP_ENV={env} | 配置文件={config_path}")
        
        return cls.from_yaml(str(config_path))
    
    def reload(self):
        """重新加载配置（更新当前实例）"""
        new_config = Config.from_environment()
        for field_name in self.model_fields:
            setattr(self, field_name, getattr(new_config, field_name))


# 全局配置实例
_config: Optional[Config] = None


def get_config() -> Config:
    """获取配置实例"""
    global _config
    if _config is None:
        _config = Config.from_environment()
    return _config


def reload_config() -> Config:
    """重新加载配置"""
    global _config
    _config = Config.from_environment()
    return _config
