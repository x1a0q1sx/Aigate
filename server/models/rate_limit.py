"""
速率限制状态 ORM 模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, ForeignKey, DateTime
from .base import Base
class RateLimitState(Base):
    __tablename__ = "rate_limits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Integer, ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    key_id = Column(Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=True)
    rpm_current = Column(Integer, nullable=False, default=0)
    rpd_current = Column(Integer, nullable=False, default=0)
    tpm_current = Column(Integer, nullable=False, default=0)
    tpd_current = Column(Integer, nullable=False, default=0)
    window_start = Column(DateTime, nullable=False, default=datetime.utcnow)
    def __repr__(self):
        return f"<RateLimit model={self.model_id} key={self.key_id} rpm={self.rpm_current}>"