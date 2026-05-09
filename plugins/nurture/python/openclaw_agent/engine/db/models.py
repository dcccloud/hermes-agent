from sqlalchemy import Column, String, Integer, JSON, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime


Base = declarative_base()


class OperationModel(Base):
    """Operation 元数据表"""
    __tablename__ = "operations"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    app = Column(String(50), nullable=False)
    handler_class = Column(String(200), nullable=False)
    parameters = Column(JSON)
    preconditions = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class TaskLogModel(Base):
    """任务执行日志表"""
    __tablename__ = "task_logs"
    
    id = Column(Integer, primary_key=True)
    task_id = Column(String(100), unique=True, nullable=False)
    device_id = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False)
    operations_result = Column(JSON)
    total_duration_ms = Column(Integer)
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
