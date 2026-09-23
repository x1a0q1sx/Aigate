"""
速率限制追踪器
追踪 RPM/RPD/TPM/TPD
"""
from typing import Dict, Optional
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from server.models.rate_limit import RateLimitState
class RateLimiter:
    def __init__(self, default_rpm: int = 60, default_tpm: int = 100000,
                 default_rpd: int = 0, default_tpd: int = 0):
        self.default_rpm = default_rpm
        self.default_tpm = default_tpm
        # P2: 日限（0 = 不限）
        self.default_rpd = default_rpd
        self.default_tpd = default_tpd
        self._cache: Dict[str, RateLimitState] = {}
    def _get_window_start(self) -> datetime:
        """获取当前分钟窗口开始"""
        now = datetime.utcnow()
        return now.replace(second=0, microsecond=0)
    def _get_day_window_start(self) -> datetime:
        """获取今日窗口开始"""
        now = datetime.utcnow()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    async def _rebase_cached_state(
        self,
        session: AsyncSession,
        cache_key: str,
    ):
        """缓存对象若已 detached(来自已关闭的 session)，merge 回当前 session。
        返回 (state, ok)；merge 失败时返回 (None, False) 并移除缓存。"""
        state = self._cache.get(cache_key)
        if state is None:
            return None, False
        try:
            if sa_inspect(state).detached:
                state = await session.merge(state)
                self._cache[cache_key] = state
            return state, True
        except Exception:
            self._cache.pop(cache_key, None)
            return None, False

    def _roll_windows(self, state) -> bool:
        """P2: 跨分钟重置 rpm/tpm、跨天重置 rpd/tpd。

        此前只重置分钟窗口，rpd/tpd 只增不减 → 日限永不生效（且永不清零）。
        返回 True 表示发生了重置（调用方需 commit）。
        """
        changed = False
        now = datetime.utcnow()
        ws = state.window_start
        if ws is None or ws < self._get_window_start():
            state.rpm_current = 0
            state.tpm_current = 0
            state.window_start = self._get_window_start()
            changed = True
        # 日窗口：用 window_start 的日期判断是否跨天（同一行同时承载分钟/日计数）
        if ws is None or ws.date() < now.date():
            state.rpd_current = 0
            state.tpd_current = 0
            changed = True
        return changed

    async def get_or_create_state(
        self,
        session: AsyncSession,
        model_id: int,
        key_id: Optional[int]
    ) -> RateLimitState:
        """获取或创建当前窗口的状态"""
        cache_key = f"{model_id}:{key_id}" if key_id else str(model_id)
        # 先查内存缓存（自动处理 detached 重绑定）
        state, ok = await self._rebase_cached_state(session, cache_key)
        if ok and state is not None:
            if self._roll_windows(state):
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
            # 并发安全插入：依赖 (model_id, key_id) 唯一索引，冲突则忽略，随后 re-query 取回已存在行。
            # P1-10: 必须用**方言版** insert 才有 on_conflict_do_nothing（通用版恒 AttributeError）。
            try:
                from server.db import IS_SQLITE
            except Exception:
                IS_SQLITE = True
            try:
                if IS_SQLITE:
                    from sqlalchemy.dialects.sqlite import insert as _dialect_insert
                else:
                    from sqlalchemy.dialects.postgresql import insert as _dialect_insert
                stmt = _dialect_insert(RateLimitState).values(
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
                # P1-10: 裸 add+commit 在并发首建时会抛 IntegrityError 冒穿请求路径。
                # 捕获后回滚并 re-query（另一协程已插入）。
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
                try:
                    await session.commit()
                    await session.refresh(state)
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(
                        select(RateLimitState).where(
                            RateLimitState.model_id == model_id,
                            RateLimitState.key_id == key_id
                        ).limit(1)
                    )
                    state = result.scalar_one_or_none()
                    if state is None:
                        raise
        self._cache[cache_key] = state
        return state
    async def check_limit(
        self,
        session: AsyncSession,
        model_id: int,
        key_id: Optional[int] = None,
        tokens: int = 0
    ) -> bool:
        """检查是否超限，True = 可用，False = 超限。

        P2: 纳入日限（rpd/tpd）——此前只查 rpm/tpm，日计数只增不减也从不生效。
        """
        state = await self.get_or_create_state(session, model_id, key_id)
        if state.rpm_current >= self.default_rpm:
            return False
        if state.tpm_current + tokens > self.default_tpm:
            return False
        # 日限：default_rpd/default_tpd 为 0 表示不限
        _rpd = getattr(self, "default_rpd", 0) or 0
        _tpd = getattr(self, "default_tpd", 0) or 0
        if _rpd and (state.rpd_current or 0) >= _rpd:
            return False
        if _tpd and (state.tpd_current or 0) + tokens > _tpd:
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