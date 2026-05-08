from typing import Optional, Dict, Any
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from openclaw_agent.engine.db.models import Base, OperationModel, TaskLogModel


class OperationRepository:
    """Operation 数据访问层"""
    
    def __init__(self, db_url: str):
        self.engine = create_engine(db_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
    
    def create(self, op_data: Dict[str, Any]) -> OperationModel:
        """创建 Operation"""
        session = self.Session()
        try:
            op = OperationModel(**op_data)
            session.add(op)
            session.commit()
            session.refresh(op)
            return op
        finally:
            session.close()
    
    def get_by_name(self, name: str) -> Optional[OperationModel]:
        """根据名称获取 Operation"""
        session = self.Session()
        try:
            return session.query(OperationModel).filter_by(name=name).first()
        finally:
            session.close()
    
    def update(self, op_id: int, op_data: Dict[str, Any]) -> OperationModel:
        """更新 Operation"""
        session = self.Session()
        try:
            op = session.query(OperationModel).filter_by(id=op_id).first()
            if op:
                for key, value in op_data.items():
                    setattr(op, key, value)
                session.commit()
                session.refresh(op)
            return op
        finally:
            session.close()
    
    def save_task_log(self, task_result: dict):
        """保存任务执行日志"""
        session = self.Session()
        try:
            log = TaskLogModel(
                task_id=task_result["task_id"],
                device_id=task_result["device_id"],
                status=task_result["status"],
                operations_result=task_result["operations_result"],
                total_duration_ms=task_result["total_duration_ms"]
            )
            session.add(log)
            session.commit()
        finally:
            session.close()


operation_repository = None
