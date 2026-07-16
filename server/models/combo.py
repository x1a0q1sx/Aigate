"""
Combos 组合 ORM 模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Boolean
from .base import Base


class Combo(Base):
    __tablename__ = "combos"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text, nullable=True, default="")
    # 策略：fallback（顺序兜底）/ round_robin（轮询）/ fusion（扇出合并，暂未实现）
    strategy = Column(String(20), nullable=False, default="fallback")
    # JSON 数组：[{"provider": "...", "model_id": "..."}, ...]
    # 存储的是完整路由键（provider_name/model_id 形式），调用时解析
    model_ids = Column(JSON, nullable=False, default=list)
    priority = Column(Integer, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Combo {self.id} {self.name} strategy={self.strategy} items={len(self.model_ids or [])}>"
