"""
管理面板登录会话 ORM 模型

历史问题：session 只存进程内存，pm2/服务每次重启全员掉线
（表现为"刚登录一会儿就过期"）。落库后重启不丢会话，
过期判断以 expires_at 为准；活跃会话在剩余时间不足一半时
滑动续期（持续使用中不会中途过期）。
"""
from datetime import datetime
from sqlalchemy import Column, String, DateTime
from .base import Base


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    token = Column(String(128), primary_key=True)
    username = Column(String(100), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)

    def __repr__(self):
        return f"<AdminSession {self.token[:8]}... user={self.username} exp={self.expires_at}>"
