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
    headers: Optional[Dict[str, str]] = None
    description: Optional[str] = None
class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_type: Optional[str] = None
    credential_type: Optional[str] = None
    oauth_code: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    description: Optional[str] = None
class ProviderResponse(BaseModel):
    id: int
    name: str
    base_url: str
    api_type: str
    credential_type: str = "api_key"
    oauth_code: Optional[str] = None
    headers: Optional[Dict[str, str]]
    description: Optional[str]
    class Config:
        from_attributes = True
class ApiKeyCreate(BaseModel):
    provider_id: int
    key: str
    label: Optional[str] = None
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
    context_length: int
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
            context_length=model.context_length,
            full_id=model.full_id,
            provider_name=model.provider.name if hasattr(model, 'provider') and model.provider else "",
            priority_boost=getattr(model, 'priority_boost', 0),
            auto_excluded=getattr(model, 'auto_excluded', False),
            request_overrides=getattr(model, 'request_overrides', None),
        )
class ModelUpdate(BaseModel):
    auto_enabled: Optional[bool] = None
    enabled: Optional[bool] = None
    input_price: Optional[float] = None
    output_price: Optional[float] = None
    success_rate: Optional[float] = None
    is_free: Optional[bool] = None
    # v2.0 新增
    priority_boost: Optional[int] = None
    auto_excluded: Optional[bool] = None
    # v3.4: per-model request overrides (headers/body_patch/model_alias)
    request_overrides: Optional[Dict[str, Any]] = None
class ModelsRefreshResponse(BaseModel):
    added: int
    updated: int
    total: int
    pricing_updated: int = 0
    metric_updated: int = 0
    pricing_sources: List[str] = []
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