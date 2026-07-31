"""
模型 ORM 模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, ForeignKey, String, Float, Boolean, Text, JSON, DateTime, UniqueConstraint
from .base import Base
class Model(Base):
    __tablename__ = "models"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_id", name="_provider_model_uc"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False)
    model_id = Column(String(200), nullable=False)
    display_name = Column(String(200), nullable=True, default="")
    input_price = Column(Float, nullable=False, default=0.0)  # 每百万 token 美元
    output_price = Column(Float, nullable=False, default=0.0)
    cache_read_input_price = Column(Float, nullable=False, default=0.0)   # 每百万 token 美元（缓存命中读，通常远低于 input_price）
    cache_write_input_price = Column(Float, nullable=False, default=0.0)  # 每百万 token 美元（缓存创建写）
    success_rate = Column(Float, nullable=True)
    avg_latency_ms = Column(Float, nullable=True)
    avg_ttft_ms = Column(Float, nullable=True)
    avg_tps = Column(Float, nullable=True)
    pricing_source = Column(String(500), nullable=True, default="")
    pricing_updated_at = Column(DateTime, nullable=True)
    is_free = Column(Boolean, nullable=False, default=False)
    auto_enabled = Column(Boolean, nullable=False, default=False)  # 是否参与 auto 选举
    enabled = Column(Boolean, nullable=False, default=True)
    supports_streaming = Column(Boolean, nullable=False, default=True)
    supports_vision = Column(Boolean, nullable=False, default=False)
    context_length = Column(Integer, nullable=False, default=4096)
    capabilities = Column(JSON, nullable=True, default=dict)
    # v0.2 新增：人工手动冷却截止时间
    manual_cooldown_until = Column(DateTime, nullable=True)
    # 自动失败冷却（真实请求失败触发）：持久化，重启后继续保留
    auto_cooldown_until = Column(DateTime, nullable=True)
    auto_fail_count = Column(Integer, nullable=False, default=0)
    # v2.0 新增：人工干预 auto 选举
    priority_boost = Column(Integer, nullable=False, default=0)    # 优先级加成 [-100, 100]
    auto_excluded = Column(Boolean, nullable=False, default=False)  # 是否强制排除
    is_manual = Column(Boolean, nullable=False, default=False)      # 是否手动添加（刷新清理失效模型时保留，不被自动删除）
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    request_overrides = Column(JSON, nullable=True, default=dict)  # v3.4 per-model request customization
    @property
    def full_id(self) -> str:
        """返回 provider_id/model_id 格式"""
        from sqlalchemy.orm import Session
        if hasattr(self, "provider") and self.provider:
            return f"{self.provider.name}/{self.model_id}"
        return self.model_id
    def __repr__(self):
        return f"<Model {self.id} {self.model_id} auto={self.auto_enabled} boost={self.priority_boost}>"
