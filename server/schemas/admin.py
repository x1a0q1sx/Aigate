"""
管理 API schema
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from .provider import ProviderResponse, ApiKeyResponse, ModelInfoResponse
class DashboardSummary(BaseModel):
    total_providers: int
    total_keys: int
    total_models: int
    auto_candidates: int
    healthy_models: int
    degraded_models: int
    rate_limited_models: int
    unhealthy_models: int
class HealthStatusItem(BaseModel):
    model_id: int
    model_full_id: str
    status: str
    latency_ms: Optional[float]
    last_checked: str
    error_message: Optional[str]
class HealthStatusResponse(BaseModel):
    items: List[HealthStatusItem]
class PlaygroundRequest(BaseModel):
    model: Optional[str] = None
    messages: List[Dict[str, str]]
    stream: Optional[bool] = False