"""
Auto 智能路由器
选举最优模型，回退机制
v2.0: 支持人工干预 priority_boost + auto_excluded
"""
import random
import time
from typing import List, Optional, AsyncGenerator, Dict, Set
from datetime import datetime
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from server.schemas.chat import ChatCompletionRequest
from server.models.model import Model
from server.models.provider import Provider
from server.models.api_key import ApiKey
from server.adapters.base_adapter import BaseAdapter
from server.adapters.openai_compat import OpenAICompatAdapter
from server.config import get_config
from .model_catalog import ModelCatalog, create_adapter_for_provider
from .health_checker import HealthChecker
from .key_manager import KeyManager
from .rate_limiter import RateLimiter
from .ranking_service import RankingService
config = get_config()
@dataclass
class RouteResult:
    success: bool
    model: Optional[Model] = None
    provider: Optional[Provider] = None
    api_key: Optional[str] = None
    adapter: Optional[BaseAdapter] = None
    fallback_count: int = 0
    error: Optional[str] = None
class AutoRouter:
    """Auto 智能路由器"""
    def __init__(
        self,
        model_catalog: ModelCatalog = None,
        health_checker: HealthChecker = None,
        key_manager: KeyManager = None,
        rate_limiter: RateLimiter = None
    ):
        self.model_catalog = model_catalog or ModelCatalog()
        self.health_checker = health_checker
        self.key_manager = key_manager
        self.rate_limiter = rate_limiter or RateLimiter(
            default_rpm=config.rate_limit.default_rpm,
            default_tpm=config.rate_limit.default_tpm
        )
        self.config = config.auto_router
        self.ranking_service = RankingService()

        # session sticky 缓存: conversation_id -> (model_id, timestamp)
        self._sticky_cache: Dict[str, tuple[int, datetime]] = {}
    def _sort_candidates(
        self,
        candidates: List[Model],
        session: Optional[AsyncSession] = None,
    ) -> List[Model]:
        """v0.2 排序：委托给 RankingService 综合打分（speed+intel+stab）"""
        if session is None:
            # 兜底：无 session 时退回旧 priority 排序
            return self._legacy_sort(candidates)
        return candidates  # 下面会调 RankingService，sorting 在 get_best_candidate 里完成
    def _legacy_sort(self, candidates: List[Model]) -> List[Model]:
        """v0.1 旧排序（兜底）"""
        status_order = {"healthy": 0, "degraded": 1, "rate_limited": 2, "unhealthy": 3, "unknown": 4}
        def sort_key(model: Model):
            status = "unknown"
            latency = 99999.0
            if self.health_checker:
                cached = self.health_checker.get_cached_status(model.id)
                if cached:
                    status = cached.status
                    latency = cached.latency_ms if cached.latency_ms else 99999.0
            status_score = status_order.get(status, 99)
            free_score = 0 if model.is_free else 1
            if not self.config.free_model_priority:
                free_score = 0
            boost_score = -model.priority_boost
            return (boost_score, status_score, free_score, latency)
        candidates.sort(key=sort_key)
        groups = {}
        for m in candidates:
            k = sort_key(m)[:3]
            groups.setdefault(k, []).append(m)
        import random
        result = []
        for k in sorted(groups.keys()):
            random.shuffle(groups[k])
            result.extend(groups[k])
        return result
    async def _rank_candidates(
        self,
        candidates: List[Model],
        session: AsyncSession,
    ) -> List:
        """v0.2: 用 RankingService 打分排序"""
        providers = {}
        for m in candidates:
            p = await session.get(Provider, m.provider_id)
            if p:
                providers[m.id] = p
        # RankingService 需要 provider dict {model.provider_id: Provider}
        prov_by_pid = {}
        for m in candidates:
            if m.provider_id not in prov_by_pid:
                p = await session.get(Provider, m.provider_id)
                if p:
                    prov_by_pid[m.provider_id] = p
        cooling = {}
        if self.health_checker:
            for m in candidates:
                if self.health_checker.is_cooling(m.id):
                    cooling[m.id] = datetime.utcnow()  # 占位，具体值不重要
        scores = await self.ranking_service.rank_all(
            session, candidates, prov_by_pid, cooling
        )
        # 把 ModelScore 按 model_id 映射回 Model 对象
        score_map = {s.model_id: s for s in scores}
        def sort_key(m: Model):
            s = score_map.get(m.id)
            if not s:
                return (1, 0, 1)
            excluded = 1 if s.excluded_reason else 0
            return (excluded, -s.final_score if s.has_full_data else 1, 0 if m.is_free else 1)
        return sorted(candidates, key=sort_key)
    def _filter_candidates(
        self,
        candidates: List[Model]
    ) -> List[Model]:
        """过滤掉不健康的、冷却中的、以及被用户排除的"""
        if not self.health_checker:
            # 即使没有 health_checker，也要过滤 auto_excluded
            return [m for m in candidates if not m.auto_excluded]
        filtered = []
        for model in candidates:
            # v2.0: 过滤用户排除的模型
            if model.auto_excluded:
                continue
            # 检查冷却
            if self.health_checker.is_cooling(model.id):
                continue
            # 检查健康状态
            cached = self.health_checker.get_cached_status(model.id)
            if cached:
                if cached.status in ["unhealthy", "rate_limited"]:
                    continue
            filtered.append(model)
        return filtered
    async def get_best_candidate(
        self,
        session: AsyncSession,
        conversation_id: Optional[str] = None,
        exclude_model_ids: Optional[Set[int]] = None
    ) -> Optional[RouteResult]:
        """获取当前最优候选"""
        exclude_model_ids = exclude_model_ids or set()
        # session sticky
        if conversation_id:
            sticky_model_id = self._get_sticky_model(conversation_id)
            if sticky_model_id:
                sticky_model = await self.model_catalog.get_by_id(session, sticky_model_id)
                if sticky_model and sticky_model.id not in exclude_model_ids and sticky_model.enabled and sticky_model.auto_enabled and not sticky_model.auto_excluded:
                    # 仍然有效，直接用
                    if not self.health_checker or not self.health_checker.is_cooling(sticky_model.id):
                        cached = self.health_checker.get_cached_status(sticky_model.id)
                        if not cached or cached.status not in ["unhealthy", "rate_limited"]:
                            # 可用
                            provider = await session.get(Provider, sticky_model.provider_id)
                            # 获取 key
                            result = await session.execute(
                                select(ApiKey)
                                .where(ApiKey.provider_id == provider.id, ApiKey.is_active == True)
                                .limit(1)
                            )
                            key = result.scalar_one_or_none()
                            if key and self.key_manager:
                                api_key = self.key_manager._crypto.decrypt(key.key_encrypted)
                                adapter = create_adapter_for_provider(provider.api_type)
                                return RouteResult(
                                    success=True,
                                    model=sticky_model,
                                    provider=provider,
                                    api_key=api_key,
                                    adapter=adapter,
                                    fallback_count=0
                                )
        # 获取所有候选
        candidates = await self.model_catalog.get_auto_candidates(session)
        if not candidates:
            return RouteResult(success=False, error="No eligible auto candidates. Add models and enable them for auto.")
        if exclude_model_ids:
            candidates = [m for m in candidates if m.id not in exclude_model_ids]
        # 过滤（严格模式：跳过冷却/不健康的）
        strict = self._filter_candidates(candidates)
        # 如果严格过滤后没有候选，且是在级联回退中，放松限制再用冷却中模型
        if not strict and exclude_model_ids:
            # 仅排除 auto_excluded，不再排除冷却/健康状态
            candidates = [m for m in candidates if not m.auto_excluded]
            print(f"[AUTO] relaxed filter: strict=0, relaxed={len(candidates)} candidates")
        else:
            candidates = strict
        if not candidates:
            return RouteResult(success=False, error="All candidates are unhealthy/rate-limited/excluded. Wait for refresh or add more models.")
        # v0.2: 用 RankingService 综合打分排序
        candidates = await self._rank_candidates(candidates, session)
        # 遍历找第一个可用的
        for candidate in candidates:
            provider = await session.get(Provider, candidate.provider_id)
            # 找一个 key
            result = await session.execute(
                select(ApiKey)
                .where(ApiKey.provider_id == candidate.provider_id, ApiKey.is_active == True)
                .limit(1)
            )
            key = result.scalar_one_or_none()
            if not key or not self.key_manager:
                continue
            # 检查限流
            can_use = await self.rate_limiter.check_limit(
                session, candidate.id, key.id
            )
            if not can_use:
                continue
            # 可用！
            api_key = self.key_manager._crypto.decrypt(key.key_encrypted)
            adapter = create_adapter_for_provider(provider.api_type)
            if conversation_id:
                self._set_sticky(conversation_id, candidate.id)
            return RouteResult(
                success=True,
                model=candidate,
                provider=provider,
                api_key=api_key,
                adapter=adapter,
                fallback_count=0
            )
        print(f"[AUTO] get_best_candidate exhausted all candidates (rate-limited or no key)")
        return RouteResult(success=False, error="All candidates are rate-limited. Try again later.")
    async def route_with_fallback(
        self,
        session: AsyncSession,
        request: ChatCompletionRequest,
        conversation_id: Optional[str] = None
    ) -> RouteResult:
        """带回退的路由"""
        tried = []
        max_retries = self.config.max_fallbacks
        fallback_count = 0
        current = await self.get_best_candidate(session, conversation_id)
        while current.success and fallback_count < max_retries:
            # 检查是否成功
            if current.success:
                return RouteResult(
                    success=True,
                    model=current.model,
                    provider=current.provider,
                    api_key=current.api_key,
                    adapter=current.adapter,
                    fallback_count=fallback_count
                )
            tried.append(current.model.id)
            if self.health_checker and current.model:
                self.health_checker.mark_cooling(
                    current.model.id,
                    self.config.cooling_period_seconds
                )
            fallback_count += 1
            current = await self.get_best_candidate(session, conversation_id)
        # 全部失败
        return current if current else RouteResult(success=False, error="All fallbacks failed")
    def _get_sticky_model(self, conversation_id: str) -> Optional[int]:
        """返回会话粘滞模型，过期则清理。"""
        cached = self._sticky_cache.get(conversation_id)
        if not cached:
            return None
        model_id, created_at = cached
        if time.time() - created_at.timestamp() > self.config.session_sticky_minutes * 60:
            self._sticky_cache.pop(conversation_id, None)
            return None
        return model_id

    def _set_sticky(self, conversation_id: str, model_id: int):
        """记录会话粘滞模型。"""
        self._sticky_cache[conversation_id] = (model_id, datetime.utcnow())

    def _get_latency(self, model) -> float:
        """从健康缓存读取延迟；返回极大值表示未知"""
        try:
            cached = self.health_checker.get_cached_status(model.id) if self.health_checker else None
            if cached and hasattr(cached, "latency_ms") and cached.latency_ms is not None:
                return float(cached.latency_ms)
        except Exception:
            pass
        return 99999.0
    async def record_usage(
        self,
        session: AsyncSession,
        model: Model,
        key_id: Optional[int],
        prompt_tokens: int,
        completion_tokens: int
    ):
        """记录使用量给限流"""
        total_tokens = prompt_tokens + completion_tokens
        await self.rate_limiter.record_usage(
            session,
            model.id,
            key_id,
            requests=1,
            tokens=total_tokens
        )