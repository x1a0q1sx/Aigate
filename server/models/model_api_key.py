"""
模型 ↔ API 密钥 多对多关联表 ORM 模型

设计意图（v3.5 模型级密钥归属）：
- 一个 provider 下可有多个 ApiKey（如 grok 三把 key）
- 每把 key 各自 list_models 返回自己能访问的模型集合
- 模型归属 = 所有"实际返回它的 key"的并集
- 请求某模型时，只从它的归属 key 集合里选 key：
  - 多个归属 key → per-model 游标轮询
  - 单个归属 key → 只用这把
  - 无归属（手动模型 / 真没被任何 key 返回）→ fallback 到 provider 第一把 active key
"""
from datetime import datetime
from sqlalchemy import Column, Integer, ForeignKey, DateTime
from .base import Base


class ModelApiKey(Base):
    __tablename__ = "model_api_keys"
    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(
        Integer,
        ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    api_key_id = Column(
        Integer,
        ForeignKey("api_keys.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 最近一次被该 key 的 list_models 返回的时间；用于过期清理
    last_seen_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<ModelApiKey model={self.model_id} key={self.api_key_id}>"
