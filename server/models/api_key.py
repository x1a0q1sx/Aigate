"""
API 密钥 ORM 模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, ForeignKey, String, Text, Boolean, DateTime
from .base import Base
class ApiKey(Base):
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False)
    key_encrypted = Column(Text, nullable=False)
    key_prefix = Column(String(20), nullable=False, default="***")
    label = Column(String(100), nullable=True, default="")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    def __repr__(self):
        return f"<ApiKey {self.id} provider={self.provider_id} prefix={self.key_prefix}>"