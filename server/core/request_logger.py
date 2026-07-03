"""
请求日志记录器
v0.2: 把 chat_completion 调用结果异步落库
"""
import time
from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from server.models.request_log import RequestLog
class RequestLogger:
    """单次请求日志写入器"""
    def __init__(self, db: AsyncSession):
        self.db = db
        self.start_ts = time.time()
    async def log(self, **kwargs) -> int:
        """记录一条日志，返回 id"""
        latency_ms = int((time.time() - self.start_ts) * 1000)
        rec = RequestLog(
            latency_ms=kwargs.pop("latency_ms", latency_ms),
            **kwargs,
        )
        self.db.add(rec)
        try:
            await self.db.commit()
            await self.db.refresh(rec)
            return rec.id
        except Exception as e:
            await self.db.rollback()
            print(f"⚠️ 写日志失败: {e}")
            return -1
