"""
健康探测器
定时探测参与 auto 的已启用模型，更新状态
v3.0: 将探测结果写入 request_logs，支持 RankingService 评分
"""
import asyncio
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from server.models.provider import Provider
from server.models.model import Model
from server.models.api_key import ApiKey
from server.models.health_check import HealthCheck
from server.models.request_log import RequestLog
from server.config import get_config
from server.adapters.base_adapter import BaseAdapter, HealthResult
from server.adapters.openai_compat import OpenAICompatAdapter
from server.adapters.anthropic_adapter import AnthropicAdapter
from .key_manager import KeyManager
from .crypto_service import get_crypto_service
import uuid
config = get_config()
@dataclass
class HealthStatus:
    model_id: int
    status: str
    latency_ms: float
    last_checked: datetime
    error_message: Optional[str]
def create_adapter_for_provider(provider: Provider) -> BaseAdapter:
    if provider.api_type == "anthropic":
        return AnthropicAdapter()
    return OpenAICompatAdapter()
class HealthChecker:
    def __init__(self):
        self.config = config.health_check
        self._status_cache: Dict[int, HealthStatus] = {}
        self._cooling: Dict[int, datetime] = {}  # model_id -> 冷却结束时间
        self._fail_count: Dict[int, int] = {}     # model_id -> 连续失败次数
        self._scheduler: Optional[AsyncIOScheduler] = None
        self.MAX_COOLING = 3600  # 最长冷却 1 小时

    def is_cooling(self, model_id: int) -> bool:
        if model_id not in self._cooling:
            return False
        now = datetime.utcnow()
        if now < self._cooling[model_id]:
            return True
        del self._cooling[model_id]
        return False

    def mark_cooling(self, model_id: int, seconds: int = 30):
        """指数退避冷却：失败次数越多冷却越久"""
        fc = self._fail_count.get(model_id, 0)
        cool = min(seconds * (2 ** fc), self.MAX_COOLING) if fc > 0 else seconds
        self._cooling[model_id] = datetime.utcnow() + timedelta(seconds=cool)

    def mark_failure(self, model_id: int):
        """记录一次失败"""
        self._fail_count[model_id] = self._fail_count.get(model_id, 0) + 1

    def mark_success(self, model_id: int):
        """成功则清零失败计数"""
        self._fail_count.pop(model_id, None)
    def get_cached_status(self, model_id: int) -> Optional[HealthStatus]:
        """获取缓存的状态"""
        return self._status_cache.get(model_id)
    def get_all_cached_status(self) -> Dict[int, HealthStatus]:
        """获取所有缓存状态"""
        return self._status_cache
    async def get_latest_from_db(
        self,
        session: AsyncSession,
        model_id: int
    ) -> Optional[HealthCheck]:
        """从数据库获取最新一次检查"""
        result = await session.execute(
            select(HealthCheck)
            .where(HealthCheck.model_id == model_id)
            .order_by(desc(HealthCheck.checked_at))
            .limit(1)
        )
        return result.scalar_one_or_none()
    async def check_model(
        self,
        session: AsyncSession,
        model: Model,
        provider: Provider,
        key_manager: KeyManager,
        write_log: bool = False,  # v3.0: 是否写入 request_logs 供 RankingService 使用
    ) -> HealthResult:
        """探测单个模型"""
        # 获取一个激活的 key
        result = await session.execute(
            select(ApiKey)
            .where(
                ApiKey.provider_id == provider.id,
                ApiKey.is_active == True
            )
            .limit(1)
        )
        key = result.scalar_one_or_none()
        if not key:
            return HealthResult(
                status="unhealthy",
                latency_ms=0,
                error_message="No active API key"
            )
        # 解密
        api_key = key_manager._crypto.decrypt(key.key_encrypted)
        # 创建适配器
        adapter = create_adapter_for_provider(provider)
        # 执行检查
        extra_headers = provider.headers if provider.headers else None
        result = await adapter.health_check(
            model.model_id,
            api_key,
            provider.base_url,
            extra_headers,
            self.config.ping_timeout_seconds
        )
        # 保存到数据库
        hc = HealthCheck(
            model_id=model.id,
            latency_ms=result.latency_ms,
            status=result.status,
            error_message=result.error_message
        )
        session.add(hc)
        # 清理旧记录
        total = await session.execute(
            select(HealthCheck).where(HealthCheck.model_id == model.id)
        )
        records = list(total.scalars().all())
        if len(records) > self.config.max_history_per_model:
            records.sort(key=lambda x: x.checked_at)
            for old in records[:-self.config.max_history_per_model]:
                await session.delete(old)
        # v3.0: 写入 request_logs，供 RankingService 计算速度和稳定性分
        if write_log:
            try:
                rl = RequestLog(
                    conversation_id=f"hc-{uuid.uuid4().hex[:12]}",
                    requested_model="auto",
                    routed_provider=provider.name,
                    routed_model=model.model_id,
                    status="success" if result.status == "healthy" else "error",
                    latency_ms=result.latency_ms,
                    prompt_tokens=0,  # 健康探测不计 token
                    completion_tokens=0,
                    http_status=200 if result.status == "healthy" else 503,
                    error_type=result.error_message[:50] if result.error_message else None,
                    error_msg=(result.error_message or "")[:500],
                    fallback_count=0,
                    user_ip="127.0.0.1",
                )
                session.add(rl)
            except Exception as e:
                print(f"[WARN] request_log write failed during health check: {e}")
        await session.commit()
        # 更新缓存
        self._status_cache[model.id] = HealthStatus(
            model_id=model.id,
            status=result.status,
            latency_ms=result.latency_ms,
            last_checked=datetime.utcnow(),
            error_message=result.error_message
        )
        return result
    async def check_all_enabled(
        self,
        session: AsyncSession,
        key_manager: KeyManager
    ) -> Dict[str, int]:
        """探测所有已启用模型（带 request_log 写入）"""
        print(f"[HealthChecker] Starting full health check...")
        # 获取所有已启用模型
        result = await session.execute(
            select(Model)
            .join(Provider, Model.provider_id == Provider.id)
            .where(
                Model.enabled == True,
                Model.auto_enabled == True,
                Model.auto_excluded == False
            )
        )
        models = list(result.scalars().all())
        stats = {"total": len(models), "healthy": 0, "degraded": 0, "rate_limited": 0, "unhealthy": 0}
        for model in models:
            provider = await session.get(Provider, model.provider_id)
            result = await self.check_model(session, model, provider, key_manager, write_log=True)
            stats[result.status] = stats.get(result.status, 0) + 1
            # 避免请求太密集
            await asyncio.sleep(0.5)
        print(f"[HealthChecker] Check complete: {stats}")
        return stats
    async def start_scheduler(self, session_factory, key_manager: KeyManager):
        """启动定时调度"""
        self._scheduler = AsyncIOScheduler()
        interval = self.config.interval_minutes
        async def scheduled_check():
            async with session_factory() as session:
                await self.check_all_enabled(session, key_manager)
        # 添加任务
        self._scheduler.add_job(
            scheduled_check,
            'interval',
            minutes=interval,
            id='health_check_full'
        )
        # 启动
        self._scheduler.start()
        # 启动后立即执行一次
        asyncio.create_task(scheduled_check())
        print(f"[HealthChecker] Scheduler started, interval={interval} minutes, write_log=True")
    def stop_scheduler(self):
        """停止调度"""
        if self._scheduler:
            self._scheduler.shutdown()