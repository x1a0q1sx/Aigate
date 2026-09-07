"""D1/D2: 下游网关密钥 ORM。

与上游服务商密钥（ApiKey）区分：GatewayKey 是客户端访问网关的钥匙。
明文只在创建时返回一次，库中仅存 sha256 哈希 + 前缀（展示用）。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime
from .base import Base


class GatewayKey(Base):
    __tablename__ = "gateway_keys"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, default="")            # 备注（给谁用）
    key_hash = Column(String(64), nullable=False, index=True)         # sha256(明文)
    key_prefix = Column(String(16), nullable=False, default="")       # 明文前 10 位，列表展示
    enabled = Column(Boolean, nullable=False, default=True)
    expires_at = Column(DateTime, nullable=True)                      # 过期时间（空=永不过期）
    rpm_limit = Column(Integer, nullable=True)                        # 每分钟请求上限（空=不限）
    # D2: 每日预算（按 UTC 日聚合 request_logs；空=不限）
    daily_token_limit = Column(Integer, nullable=True)
    daily_cost_limit_usd = Column(Float, nullable=True)
    over_limit_action = Column(String(20), nullable=False, default="reject")  # reject=429 / warn=放行
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def to_info(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "enabled": bool(self.enabled),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "rpm_limit": self.rpm_limit,
            "daily_token_limit": self.daily_token_limit,
            "daily_cost_limit_usd": self.daily_cost_limit_usd,
            "over_limit_action": self.over_limit_action,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
