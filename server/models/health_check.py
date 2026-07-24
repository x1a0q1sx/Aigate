"""
健康检查记录 ORM 模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, ForeignKey, Float, String, Text, DateTime
from .base import Base
class HealthCheck(Base):
    __tablename__ = "health_checks"
    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Integer, ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    latency_ms = Column(Float, nullable=True)
    status = Column(String(20), nullable=False, default="unknown")
    # 状态: healthy / degraded / rate_limited / unhealthy / unknown
    error_message = Column(Text, nullable=True, default="")
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    def __repr__(self):
        return f"<HealthCheck model={self.model_id} status={self.status} latency={self.latency_ms}ms>"