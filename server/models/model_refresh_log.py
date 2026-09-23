"""模型刷新日志：每次 refresh_models_from_provider 落一行，分析页可查详情。"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Index
from .base import Base


class ModelRefreshLog(Base):
    __tablename__ = "model_refresh_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    trigger = Column(String(20), default="manual")          # manual（当前无定时刷新）/ scheduled
    provider_id = Column(Integer, nullable=True)
    provider_name = Column(String(100), default="")
    ok = Column(Boolean, default=True)
    duration_ms = Column(Integer, default=0)
    added = Column(Integer, default=0)
    updated = Column(Integer, default=0)
    removed = Column(Integer, default=0)
    total = Column(Integer, default=0)
    pricing_updated = Column(Integer, default=0)
    metric_updated = Column(Integer, default=0)
    pricing_source = Column(String(500), nullable=True)       # 定价源 URL
    error = Column(Text, nullable=True)                       # 错误信息（含定价源失败）
    # JSON 数组（模型增删明细，供详情页展开；行数小不设归档）
    added_models = Column(Text, nullable=True)
    removed_models = Column(Text, nullable=True)
    # v4.3: 来源追踪（online=上游列表 / seed=静态种子兜底 / pricing=定价回退建模）
    list_source = Column(String(20), default="unknown")
    list_note = Column(Text, nullable=True)                    # 说明（如"seed 兜底：N 个模型"）

    __table_args__ = (
        Index("idx_mrl_created", "created_at"),
    )

    def __repr__(self):
        return f"<ModelRefreshLog provider={self.provider_name} ok={self.ok} +{self.added}/-{self.removed}>"
