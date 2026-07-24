"""
模型智力静态参考表
v0.2: 管理员可编辑
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime
from .base import Base
class IntelligenceStatic(Base):
    __tablename__ = "intelligence_static"
    id = Column(Integer, primary_key=True, autoincrement=True)
    pattern = Column(String(100), nullable=False, unique=True)  # 通配 pattern, e.g. "claude-opus-4-*"
    score = Column(Integer, nullable=False)  # 0-100
    tier = Column(String(8), nullable=False)  # S/A/B/C
    source = Column(String(20), nullable=False, default="arena")  # manual(手工校准) / arena(Arena 同步)
    notes = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)
