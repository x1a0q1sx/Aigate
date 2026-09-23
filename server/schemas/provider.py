"""
Provider 相关 schema
"""
from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel
from server.models.model import Model
class ProviderCreate(BaseModel):
    name: str
    base_url: str
    api_type: str = "openai_compat"
    credential_type: str = "api_key"  # api_key / free_tier / oauth
    oauth_code: Optional[str] = None   # 当 credential_type=oauth 时填 OAuthRegistry code（如 "claude_code"）
    oauth_owner: Optional[str] = None  # v4.2: 多账号连接时指定路由账号（owner）；空=自动
    enabled: bool = True               # v4.0: 服务商启用/禁用开关
    headers: Optional[Dict[str, str]] = None
    proxy_url: Optional[str] = None
    proxy_enabled: bool = False
    description: Optional[str] = None
    # v4.2: 按服务商的定时模型刷新（自定义频率，分钟）
    model_refresh_enabled: bool = False
    model_refresh_interval_minutes: int = 60
    # v4.3: 指纹过滤（u1s1 竞品名/工具名遮蔽），默认开
    fingerprint_filter_enabled: bool = True
class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_type: Optional[str] = None
    credential_type: Optional[str] = None
    oauth_code: Optional[str] = None
    oauth_owner: Optional[str] = None
    enabled: Optional[bool] = None     # v4.0: 服务商启用/禁用开关
    headers: Optional[Dict[str, str]] = None
    proxy_url: Optional[str] = None
    proxy_enabled: Optional[bool] = None
    description: Optional[str] = None
    model_refresh_enabled: Optional[bool] = None
    model_refresh_interval_minutes: Optional[int] = None
    fingerprint_filter_enabled: Optional[bool] = None
class ProviderResponse(BaseModel):
    id: int
    name: str
    base_url: str
    api_type: str
    credential_type: str = "api_key"
    oauth_code: Optional[str] = None
    oauth_owner: Optional[str] = None  # v4.2: 指定路由账号；空=自动（__default→任一 active）
    enabled: bool = True               # v4.0: 服务商启用/禁用开关
    headers: Optional[Dict[str, str]]
    proxy_url: Optional[str] = None
    proxy_enabled: bool = False
    description: Optional[str]
    # v4.2: 按服务商定时模型刷新
    model_refresh_enabled: bool = False
    model_refresh_interval_minutes: int = 60
    model_refresh_next_at: Optional[datetime] = None
    model_refresh_last_at: Optional[datetime] = None
    # v4.3: 指纹过滤开关（详情浮窗可切换，默认开）
    fingerprint_filter_enabled: bool = True
    # 详情浮窗只读展示
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    class Config:
        from_attributes = True
class ApiKeyCreate(BaseModel):
    provider_id: int
    key: str
    label: Optional[str] = None
class ApiKeyToggle(BaseModel):
    """启用/停用某把服务商密钥；不传 is_active 则按当前状态取反。"""
    is_active: Optional[bool] = None
class ApiKeyResponse(BaseModel):
    id: int
    provider_id: int
    key_prefix: Optional[str]
    label: Optional[str]
    is_active: bool
    class Config:
        from_attributes = True
class ModelInfoResponse(BaseModel):
    id: int
    provider_id: int
    model_id: str
    display_name: Optional[str]
    input_price: float
    output_price: float
    cache_read_input_price: float = 0.0
    cache_write_input_price: float = 0.0
    success_rate: Optional[float] = None
    avg_latency_ms: Optional[float] = None
    avg_ttft_ms: Optional[float] = None
    avg_tps: Optional[float] = None
    pricing_source: Optional[str] = ""
    pricing_updated_at: Optional[datetime] = None
    is_free: bool
    auto_enabled: bool
    enabled: bool
    supports_streaming: bool
    supports_vision: bool
    supports_reasoning_effort: Optional[bool] = None
    context_length: int
    context_source: Optional[str] = ""
    capability_source: Optional[str] = ""
    # v4.3 能力感知路由：模态/输出容量/实测窗口（None=未知）
    input_modalities: Optional[list] = None
    max_output_tokens: Optional[int] = None
    observed_context_limit: Optional[int] = None
    # v4.4 订阅制上游倍率（CodeBuddy/Qoder 按 credit 倍率计费，无 USD 单价）
    price_ratio: Optional[float] = None
    price_ratio_source: Optional[str] = ""
    full_id: str
    provider_name: str = ""
    # v2.0 新增
    priority_boost: int = 0
    auto_excluded: bool = False
    # v3.4: per-model request overrides (CCSwitch style)
    request_overrides: Optional[Dict[str, Any]] = None
    # 延迟信息（来自最新健康检查）
    latency_ms: Optional[float] = None
    health_status: Optional[str] = None
    # 冷却信息
    cooldown_until: Optional[str] = None  # ISO 时间字符串，非空表示正在冷却
    fail_count: int = 0
    # 所属分组（combo 名称列表，由列表接口附带；不在 ORM 上，from_orm 不填）
    combos: Optional[list] = None
    class Config:
        from_attributes = True
    @classmethod
    def from_orm(cls, model: Model):
        return cls(
            id=model.id,
            provider_id=model.provider_id,
            model_id=model.model_id,
            display_name=model.display_name,
            input_price=model.input_price,
            output_price=model.output_price,
            cache_read_input_price=getattr(model, 'cache_read_input_price', 0) or 0,
            cache_write_input_price=getattr(model, 'cache_write_input_price', 0) or 0,
            success_rate=getattr(model, 'success_rate', None),
            avg_latency_ms=getattr(model, 'avg_latency_ms', None),
            avg_ttft_ms=getattr(model, 'avg_ttft_ms', None),
            avg_tps=getattr(model, 'avg_tps', None),
            pricing_source=getattr(model, 'pricing_source', '') or '',
            pricing_updated_at=getattr(model, 'pricing_updated_at', None),
            is_free=model.is_free,
            auto_enabled=model.auto_enabled,
            enabled=model.enabled,
            supports_streaming=model.supports_streaming,
            supports_vision=model.supports_vision,
            supports_reasoning_effort=getattr(model, "supports_reasoning_effort", None),
            context_length=model.context_length,
            # P1-6 声明了却从未透传的来源标记 / v4.3 能力字段——一并补上
            context_source=getattr(model, "context_source", "") or "",
            capability_source=getattr(model, "capability_source", "") or "",
            input_modalities=getattr(model, "input_modalities", None),
            max_output_tokens=getattr(model, "max_output_tokens", None),
            observed_context_limit=getattr(model, "observed_context_limit", None),
            price_ratio=getattr(model, "price_ratio", None),
            price_ratio_source=getattr(model, "price_ratio_source", "") or "",
            full_id=model.full_id,
            provider_name=model.provider.name if hasattr(model, 'provider') and model.provider else "",
            priority_boost=getattr(model, 'priority_boost', 0),
            auto_excluded=getattr(model, 'auto_excluded', False),
            request_overrides=getattr(model, 'request_overrides', None),
        )
class ModelUpdate(BaseModel):
    display_name: Optional[str] = None
    auto_enabled: Optional[bool] = None
    enabled: Optional[bool] = None
    input_price: Optional[float] = None
    output_price: Optional[float] = None
    cache_read_input_price: Optional[float] = None
    cache_write_input_price: Optional[float] = None
    success_rate: Optional[float] = None
    is_free: Optional[bool] = None
    # v2.0 新增
    priority_boost: Optional[int] = None
    auto_excluded: Optional[bool] = None
    supports_reasoning_effort: Optional[bool] = None
    context_length: Optional[int] = None  # P1-6: 手动改窗口 → context_source=manual（刷新不覆盖）
    # v4.3: 手动能力编辑（写入即 capability_source=manual，刷新/同步永不覆盖）
    supports_vision: Optional[bool] = None
    input_modalities: Optional[List[str]] = None
    max_output_tokens: Optional[int] = None
    # v4.4: 手动倍率（订阅制上游；写入即 price_ratio_source=manual，刷新不覆盖）
    price_ratio: Optional[float] = None
    # 清空倍率（回到"未知"）：price_ratio 传 null 时无法与"未修改"区分，故单列一个开关
    clear_price_ratio: bool = False
    # v3.4: per-model request overrides (headers/body_patch/model_alias)
    request_overrides: Optional[Dict[str, Any]] = None
class ModelsRefreshResponse(BaseModel):
    added: int
    updated: int
    total: int
    removed: int = 0
    pricing_updated: int = 0
    metric_updated: int = 0
    pricing_sources: List[str] = []
    added_details: List[Dict[str, Any]] = []    # [{"provider_id":int,"provider_name":str,"models":[{"model_id":str,"display_name":str}]}]
    removed_details: List[Dict[str, Any]] = []  # 同上，仅当 remove_missing_models=true 时才有内容
# v2.0 新增：测速相关 schema
class PingResult(BaseModel):
    model_id: int
    model_full_id: str
    status: str
    latency_ms: float
    error_message: Optional[str] = None
    checked_at: str
class PingAllResponse(BaseModel):
    total: int
    healthy: int
    degraded: int
    rate_limited: int
    unhealthy: int
    results: List[PingResult]
class LatencyStatsResponse(BaseModel):
    fastest: List[PingResult]
    slowest: List[PingResult]
    average_latency_ms: float
    total_models: int
