"""
服务商 ORM 模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Boolean
from .base import Base
class Provider(Base):
    __tablename__ = "providers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    base_url = Column(String(500), nullable=False)
    api_type = Column(String(50), nullable=False, default="openai_compat")
    # v4.0: 服务商启用/禁用开关。禁用后路由层跳过该服务商的所有模型，
    # 但保留模型配置与组合中的排列顺序（不删除任何数据）。
    enabled = Column(Boolean, nullable=False, default=True)
    # v3.0 新增：凭据类型 — api_key（传统密钥）/ free_tier（免费层 GitHub Models）/ oauth（订阅转API）
    credential_type = Column(String(20), nullable=False, default="api_key")
    # v3.1 新增：oauth_code — 当 credential_type=oauth 时，明确指向 OAuthRegistry 的 provider code
    #（如 "claude_code"/"codex"/"codebuddy_cn"）。若为空，路由层会回退尝试用 provider.name 匹配 registry code。
    # 避免歧义：用户可以给 OAuth 类型的 provider 起任意名字（如 "ClaudeCode 迷你"），不会影响 OAuth 凭证拾取。
    oauth_code = Column(String(50), nullable=True, default=None)
    headers = Column(JSON, nullable=True, default=dict)
    description = Column(Text, nullable=True, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    def __repr__(self):
        return f"<Provider {self.id} {self.name}>"