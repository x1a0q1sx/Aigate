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
    # OAuth 路由账号：同一 oauth_code 下多条连接（多账号）时，指定本服务商走哪个 owner。
    # 空 = 自动：优先 __default，缺失时回退该服务商任一 active 连接
    #（连接可被改名/删除，写死 __default 会让路由直接报"未连接"）。
    oauth_owner = Column(String(100), nullable=True, default=None)
    # 按服务商的定时模型刷新：独立于 config.yaml 全局开关。勾选者从全局 scheduled
    # 批量中排除，由调度器每分钟的 tick 按各自频率到点触发。
    model_refresh_enabled = Column(Boolean, nullable=False, default=False)
    model_refresh_interval_minutes = Column(Integer, nullable=False, default=60)
    model_refresh_next_at = Column(DateTime, nullable=True, default=None)
    model_refresh_last_at = Column(DateTime, nullable=True, default=None)
    # 指纹过滤（v4.3）：上游按「AI agent 客户端指纹」拦截时（u1s1：竞品客户端名 +
    # apply_patch/update_plan 工具名），出站前做语义等价改写、响应侧还原。
    # 仅对档案里带 blocked_tool_names/neutralize_competitor_tokens 的域名生效。默认开。
    fingerprint_filter_enabled = Column(Boolean, nullable=False, default=True)
    headers = Column(JSON, nullable=True, default=dict)
    proxy_url = Column(String(500), nullable=True, default=None)
    # Kept briefly for import compatibility; new configurations use proxy_enabled only.
    # When enabled, use the configured proxy pool even if the global pool switch is off.
    proxy_enabled = Column(Boolean, nullable=False, default=False)
    description = Column(Text, nullable=True, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    def __repr__(self):
        return f"<Provider {self.id} {self.name}>"
