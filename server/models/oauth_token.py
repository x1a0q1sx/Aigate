"""
OAuth Token 持久化模型
存储每个 (provider_code, AIGate_user_id) 一组的 access_token / refresh_token
加密字段：access_token, refresh_token（用 CryptoService 加密）
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Index
from .base import Base


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_code = Column(String(50), nullable=False)         # claude_code / codex / github_copilot ...
    # 多用户：可绑定到 AIGate 内部账号，无账号时叫 "__default"
    owner = Column(String(100), nullable=False, default="__default")
    # 加密字段
    access_token_enc = Column(Text, nullable=False)
    refresh_token_enc = Column(Text, nullable=True)
    # 明文 metadata
    token_type = Column(String(20), default="Bearer")
    scope = Column(String(500), default="")
    expires_at = Column(DateTime, nullable=True)               # access_token 到期时间
    refresh_expires_at = Column(DateTime, nullable=True)       # refresh_token 到期时间（部分 provider 有）
    # 状态
    is_active = Column(Boolean, default=True)
    last_refreshed_at = Column(DateTime, nullable=True)
    last_error = Column(String(500), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_oauth_provider_owner", "provider_code", "owner"),
    )

    def __repr__(self):
        return f"<OAuthToken provider={self.provider_code} owner={self.owner} active={self.is_active}>"
