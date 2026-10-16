"""上游「每日限额」冷却记录（(账号×模型) 粒度，2026-10-16）。

背景：CodeBuddy / workbuddy 国服与国际服在每日用量触顶后，推理接口返回
HTTP 429 + 结构化文案，**自带精确重置时间**（见 core/quota_limit.py 的报文样例）。
语义是 (账号 × 模型) 粒度的每日限额——上游明说「您也可以切换其他模型继续使用」，
所以不能记成账号级冷却（那会误伤该账号的其他模型）。

记录由 oauth_client.note_request_result 在识别到限额文案时 upsert；自动选号按
(provider_code, owner, model_id) 查未过期记录并跳过对应账号；模型页 / OAuth 连接页
按 provider_id / provider_code 聚合展示。reset_at 过期后记录自动失效
（查询侧过滤 reset_at > now），无需清理任务。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Index
from .base import Base


class QuotaLimit(Base):
    __tablename__ = "quota_limits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_code = Column(String(50), nullable=False, default="")  # oauth code（写入时必填）
    provider_id = Column(Integer, nullable=True)          # providers.id（尽力解析，模型页聚合用）
    owner = Column(String(100), nullable=False, default="")  # OAuth 账号（owner）
    model_id = Column(String(200), nullable=False)         # 被限额的模型（裸 model_id）
    reset_at = Column(DateTime, nullable=False)            # 上游给出的重置时间（naive UTC）
    note = Column(String(500), default="")                 # 触发时的错误摘要（排障用）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("uq_quota_limits_pair", "provider_code", "owner", "model_id", unique=True),
        Index("idx_quota_limits_reset", "reset_at"),
        Index("idx_quota_limits_code_model", "provider_code", "model_id"),
    )

    def __repr__(self):
        return (f"<QuotaLimit provider={self.provider_code} owner={self.owner} "
                f"model={self.model_id} until={self.reset_at}>")
