"""
路由配置表（权重 / 锁定）+ 审计日志
v0.2
"""
from datetime import datetime
from sqlalchemy import Column, Integer, Numeric, String, JSON, DateTime, ForeignKey
from .base import Base
class RoutingWeights(Base):
    __tablename__ = "routing_weights"
    id = Column(Integer, primary_key=True)  # 永远 = 1
    w_speed = Column(Numeric(4, 3), nullable=False, default=0.30)
    w_intel = Column(Numeric(4, 3), nullable=False, default=0.50)
    w_stab = Column(Numeric(4, 3), nullable=False, default=0.20)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)
class RoutingPin(Base):
    __tablename__ = "routing_pin"
    id = Column(Integer, primary_key=True)  # 永远 = 1
    pinned_model_id = Column(Integer, ForeignKey("models.id"), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)
class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String(64), nullable=True)
    action = Column(String(64), nullable=False)
    target_id = Column(Integer, nullable=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
