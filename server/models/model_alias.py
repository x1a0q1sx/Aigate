"""E1: 模型别名 ORM。

客户端请求 model=alias → 网关改写为 target（可以是 服务商/模型、combo:名称 等
一切请求入口能解析的名字）。解决公益站命名五花八门的问题。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from .base import Base


class ModelAlias(Base):
    __tablename__ = "model_aliases"
    id = Column(Integer, primary_key=True, autoincrement=True)
    alias = Column(String(200), nullable=False, unique=True)   # 客户端请求看到的模型名
    target = Column(String(400), nullable=False)               # 实际路由目标（provider/model 或 combo:xxx）
    enabled = Column(Boolean, nullable=False, default=True)
    note = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def to_info(self) -> dict:
        return {
            "id": self.id,
            "alias": self.alias,
            "target": self.target,
            "enabled": bool(self.enabled),
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
