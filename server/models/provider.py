"""
服务商 ORM 模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Boolean
from sqlalchemy.orm import validates
from sqlalchemy.types import TypeDecorator

from server.core.header_values import normalize_map
from .base import Base


class HeaderJSON(TypeDecorator):
    """providers.headers 专用 JSON 列：读写两侧都把值强制成 str。

    2026-09-27 生产事故：导入路径写入 {"x-video-timeout": 1800}（int），
    导致 ① 响应模型校验 500 打挂服务商页/模型页；② httpx 出站直接
    TypeError（该服务商所有推理请求失败）。JSON 列本身不校验值类型，
    故在 ORM 层兜底：**任何写入路径**（API/导入/恢复/脚本）都存不进非 str 值，
    **任何读取路径**读到的历史脏数据也自动变干净。
    """
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return normalize_map(value, strict=False)

    def process_result_value(self, value, dialect):
        return normalize_map(value, strict=False)


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
    # 自定义出站头（provider 级）。值必须恒为 str（httpx 硬要求）——
    # 用 HeaderJSON 在 ORM 层双向兜底，详见该类型 docstring。
    headers = Column(HeaderJSON, nullable=True, default=dict)
    proxy_url = Column(String(500), nullable=True, default=None)
    # Kept briefly for import compatibility; new configurations use proxy_enabled only.
    # When enabled, use the configured proxy pool even if the global pool switch is off.
    proxy_enabled = Column(Boolean, nullable=False, default=False)
    description = Column(Text, nullable=True, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    @validates("headers")
    def _normalize_headers(self, key, value):
        """赋值即归一化：TypeDecorator 只在写库/读库时生效，而导入路径会
        「先赋值、后在同会话内直接读」——不在此处兜底，同一进程内仍会
        读到 int 值（事故当天导入后立刻列表就 500，正是这个时序）。"""
        return normalize_map(value, strict=False)

    def __repr__(self):
        return f"<Provider {self.id} {self.name}>"
