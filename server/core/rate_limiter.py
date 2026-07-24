"""
速率限制追踪器
追踪 RPM/RPD/TPM/TPD
"""
from typing import Dict, Optional
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from server.models.rate_limit import RateLimitState
class RateLimiter:
    def __init__(self, default_rpm: int = 60, default_tpm: int = 100000):
        self.default_rpm = default_rpm
        self.default_tpm = default_tpm
        self._cache: Dict[int, RateLimitState] = {}
    def _get_window_start(self) -> datetime:
        """获取当前分钟窗口开始"""
        now = datetime.utcnow()
        return now.replace(second=0, microsecond=0)
    def _get_day_window_start(self) -> datetime:
        """获取今日窗口开始"""
        now = datetime.utcnow()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    async def get_or_create_state(
        self,
        session: AsyncSession,
        model_id: int,
        key_id: Optional[int]
    ) -> RateLimitState:
        """获取或创建当前窗口的状态"""
        cache_key = f"{model_id}:{key_id}" if key_id else model_id
        # 先查内存缓存
        if cache_key in self._cache:
            state = self._cache[cache_key]
            # 检查窗口是否过期
            now = datetime.utcnow()
            if state.window_start < self._get_window_start():
                # 新窗口，重置
                state.rpm_current = 0
                state.tpm_current = 0
                state.window_start = self._get_window_start()
                await session.commit()
            return state
        # 查数据库（limit(1)：即使历史存在重复行也不会 MultipleResultsFound）
        result = await session.execute(
            select(RateLimitState).where(
                RateLimitState.model_id == model_id,
                RateLimitState.key_id == key_id
            ).limit(1)
        )
        state = result.scalar_one_or_none()
        if not state:
            # 并发安全插入：依赖 (model_id, key_id) 唯一索引，冲突则忽略，随后 re-query 取回已存在行
            from sqlalchemy import insert
            try:
                stmt = insert(RateLimitState).values(
                    model_id=model_id,
                    key_id=key_id,
                    rpm_current=0,
                    rpd_current=0,
                    tpm_current=0,
                    tpd_current=0,
                    window_start=self._get_window_start()
                ).on_conflict_do_nothing(index_elements=["model_id", "key_id"])
                await session.execute(stmt)
                await session.commit()
            except Exception:
                # 唯一索引尚未就绪（未迁移）时的降级：回滚后直接查回
                await session.rollback()
            result = await session.execute(
                select(RateLimitState).where(
                    RateLimitState.model_id == model_id,
                    RateLimitState.key_id == key_id
                ).limit(1)
            )
            state = result.scalar_one_or_none()
            if not state:
                state = RateLimitState(
                    model_id=model_id,
                    key_id=key_id,
                    rpm_current=0,
                    rpd_current=0,
                    tpm_current=0,
                    tpd_current=0,
                    window_start=self._get_window_start()
                )
                session.add(state)
                await session.commit()
                await session.refresh(state)
        self._cache[cache_key] = state
        return state
    async def check_limit(
        self,
        session: AsyncSession,
        model_id: int,
        key_id: Optional[int] = None,
        tokens: int = 0
    ) -> bool:
        """检查是否超限，True = 可用，False = 超限"""
        state = await self.get_or_create_state(session, model_id, key_id)
        if state.rpm_current >= self.default_rpm:
            return False
        if state.tpm_current + tokens > self.default_tpm:
            return False
        return True
    async def record_usage(
        self,
        session: AsyncSession,
        model_id: int,
        key_id: Optional[int] = None,
        requests: int = 1,
        tokens: int = 0
    ) -> None:
        """记录使用量"""
        state = await self.get_or_create_state(session, model_id, key_id)
        state.rpm_current += requests
        state.rpd_current += requests
        state.tpm_current += tokens
        state.tpd_current += tokens
        await session.commit()
    async def cleanup_old(self, session: AsyncSession) -> int:
        """清理超过一天的旧记录"""
        cutoff = datetime.utcnow() - timedelta(days=1)
        result = await session.execute(
            delete(RateLimitState).where(RateLimitState.window_start < cutoff)
        )
        await session.commit()
        return result.rowcount