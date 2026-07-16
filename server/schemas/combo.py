"""
Combos 组合 schema
"""
from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, field_validator


class ComboItem(BaseModel):
    """组合中的一个模型条目"""
    provider: str          # provider name
    model_id: str          # 模型 ID（不含 provider/ 前缀）
    full_id: Optional[str] = None  # "provider/model_id"，方便前端直接显示


class ComboCreate(BaseModel):
    name: str
    description: Optional[str] = None
    strategy: str = "fallback"     # fallback / round_robin / fusion
    model_ids: List[Dict[str, Any]] = []   # [{"provider":..., "model_id":...}]
    priority: int = 0
    enabled: bool = True

    @field_validator("strategy")
    @classmethod
    def _validate_strategy(cls, v: str) -> str:
        v = (v or "fallback").lower()
        if v not in ("fallback", "round_robin", "fusion"):
            raise ValueError("strategy must be one of fallback/round_robin/fusion")
        return v

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name cannot be empty")
        if v.lower().startswith("combo:") or v.lower() == "auto":
            raise ValueError("name cannot start with 'combo:' or be 'auto'")
        return v


class ComboUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    strategy: Optional[str] = None
    model_ids: Optional[List[Dict[str, Any]]] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None


class ComboResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    strategy: str
    model_ids: List[Any]
    priority: int
    enabled: bool
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class ComboListResponse(BaseModel):
    items: List[ComboResponse]
    total: int
