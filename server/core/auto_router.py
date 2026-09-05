"""
Auto 智能路由器
选举最优模型，回退机制
v2.0: 支持人工干预 priority_boost + auto_excluded
"""
import itertools
import random
import time
from typing import List, Optional, AsyncGenerator, Dict, Set
from datetime import datetime
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, inspect as sa_inspect
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
    key_id: Optional[int] = None
    adapter: Optional[BaseAdapter] = None
    # OAuth ????????? adapter????????
    extra_headers: Optional[dict] = None
    fallback_count: int = 0
    error: Optional[str] = None
    selection_reason: Optional[str] = None
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
        conversation_id: Optional[str] = None,
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
        ordered = sorted(candidates, key=sort_key)
        if conversation_id:
            from .route_decision import capture_candidates

            snapshots = []
            for rank, model in enumerate(ordered, start=1):
                score = score_map.get(model.id)
                provider = prov_by_pid.get(model.provider_id)
                cached = self.health_checker.get_cached_status(model.id) if self.health_checker else None
                snapshots.append({
                    "rank": rank,
                    "model_pk": model.id,
                    "provider": getattr(provider, "name", None) if provider else None,
                    "model": getattr(model, "model_id", str(model.id)),
                    "eligible": not bool(score and score.excluded_reason),
                    "final_score": score.final_score if score else None,
                    "speed_score": getattr(score, "speed_score", None) if score else None,
                    "intel_score": getattr(score, "intel_score", None) if score else None,
                    "intel_source": getattr(score, "intel_source", None) if score else None,
                    "stability_score": getattr(score, "stab_score", None) if score else None,
                    "avg_latency_ms": getattr(score, "avg_ms", None) if score else None,
                    "success_rate": getattr(score, "success_rate", None) if score else None,
                    "priority_boost": getattr(model, "priority_boost", 0) or 0,
                    "health": getattr(cached, "status", None),
                    "skip_reason": score.excluded_reason if score else None,
                })
            capture_candidates(conversation_id, snapshots)
        return ordered

    def _candidate_skip_reason(self, model: Model) -> Optional[str]:
        if model.auto_excluded:
            return "manually excluded from Auto"
        if self.health_checker and self.health_checker.is_cooling(model.id):
            return "model is cooling down"
        cached = self.health_checker.get_cached_status(model.id) if self.health_checker else None
        if cached and cached.status in ["unhealthy", "rate_limited"]:
            return f"health status: {cached.status}"
        return None
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
            if self._candidate_skip_reason(model):
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
                            # v4.0: 服务商被禁用 → sticky 失效，走正常候选选举
                            if provider is not None and getattr(provider, "enabled", True):
                                # Free Tier / OAuth — key 可空
                                api_key = None
                                key_id_for_rate = None
                                if getattr(provider, "credential_type", "api_key") in ("free_tier", "oauth") or provider.api_type == "atomcode":
                                    if provider.credential_type == "oauth":
                                        try:
                                            from server.core.oauth_client import get_oauth_client
                                            _oc = getattr(provider, "oauth_code", None) or provider.name
                                            api_key = await get_oauth_client().pick_access_token(_oc, session)
                                        except Exception:
                                            api_key = None
                                        if not api_key:
                                            return None   # OAuth 未连接，放走
                                    else:
                                        api_key = ""
                                else:
                                    # 获取 key（v3.5：按模型归属 key 集合选）
                                    try:
                                        from server.core.key_rotator import get_key_rotator
                                        _picked = await get_key_rotator().pick_key_for_model(session, sticky_model)
                                    except Exception:
                                        _picked = None
                                    if _picked and _picked[0] is not None:
                                        key_id_for_rate, api_key = _picked
                                    else:
                                        return None
                                if not api_key and api_key != "":
                                    return None
                                # 防 MissingGreenlet：provider 属性可能已过期
                                try:
                                    await session.refresh(provider, attribute_names=["api_type", "credential_type", "name", "base_url", "headers", "oauth_code"])
                                except Exception:
                                    pass
                                adapter = create_adapter_for_provider(provider.api_type)
                                _extra = {"__oauth": True} if (getattr(provider, "credential_type", "") == "oauth") else None
                                if getattr(provider, "proxy_enabled", False):
                                    _extra = {**(_extra or {}), "__proxy_force": True}
                                from .route_decision import capture_candidates, mark_selected
                                capture_candidates(conversation_id, [{
                                    "rank": 1,
                                    "model_pk": sticky_model.id,
                                    "provider": provider.name,
                                    "model": sticky_model.model_id,
                                    "eligible": True,
                                    "selected": True,
                                    "selection_reason": "session sticky",
                                }])
                                mark_selected(
                                    conversation_id,
                                    provider=provider.name,
                                    model=sticky_model.model_id,
                                    model_pk=sticky_model.id,
                                    reason="session sticky",
                                )
                                return RouteResult(
                                    success=True,
                                    model=sticky_model,
                                    provider=provider,
                                    api_key=api_key,
                                    key_id=key_id_for_rate,
                                    adapter=adapter,
                                    fallback_count=0,
                                    extra_headers=_extra,
                                    selection_reason="session sticky",
                                )
        # 获取所有候选
        candidates = await self.model_catalog.get_auto_candidates(session)
        if not candidates:
            return RouteResult(success=False, error="No eligible auto candidates. Add models and enable them for auto.")
        if exclude_model_ids:
            candidates = [m for m in candidates if m.id not in exclude_model_ids]
        if conversation_id:
            from .route_decision import capture_candidates
            capture_candidates(conversation_id, [
                {
                    "model_pk": model.id,
                    "model": model.model_id,
                    "eligible": False,
                    "skip_reason": self._candidate_skip_reason(model),
                }
                for model in candidates
                if self._candidate_skip_reason(model)
            ])
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
        candidates = await self._rank_candidates(candidates, session, conversation_id)
        # FIX: 移除并发轮转(_rr_counter)。轮转会把人工 priority_boost 权重高的候选
        # 整体旋转到后面，导致 fallback 先尝试低权重/已冷却模型而错过可用渠道。
        # priority_boost 是人工干预权重，应严格按排序降序尝试。
        # 遍历找第一个可用的
        for candidate in candidates:
            # 防 MissingGreenlet：rate_limiter.check_limit 撞库锁时会走 session.rollback()，
            # rollback 无视 expire_on_commit=False 强制过期 session 内所有 ORM 对象（含本轮/后续
            # candidate）。若此处不先 greenlet-safe 刷新，后续同步访问 candidate.provider_id /
            # candidate.id 会触发隐式懒加载 → MissingGreenlet 崩溃。
            try:
                if sa_inspect(candidate).expired:
                    await session.refresh(candidate)
            except Exception:
                try:
                    candidate = await session.merge(candidate)
                except Exception:
                    pass
            provider = await session.get(Provider, candidate.provider_id)
            if provider is None or not getattr(provider, "enabled", True):
                if conversation_id:
                    from .route_decision import mark_candidate_skipped
                    mark_candidate_skipped(
                        conversation_id,
                        model_pk=candidate.id,
                        model=candidate.model_id,
                        reason="provider disabled or missing",
                    )
                continue
            # Free Tier / OAuth providers — key 可空
            api_key = None
            key = None
            if getattr(provider, "credential_type", "api_key") in ("free_tier", "oauth") or provider.api_type == "atomcode":
                if provider.credential_type == "oauth":
                    try:
                        from server.core.oauth_client import get_oauth_client
                        _oc = getattr(provider, "oauth_code", None) or provider.name
                        api_key = await get_oauth_client().pick_access_token(_oc, session)
                    except Exception:
                        api_key = None
                    if not api_key:
                        # OAuth 未连接，跳过此候选
                        if conversation_id:
                            from .route_decision import mark_candidate_skipped
                            mark_candidate_skipped(
                                conversation_id,
                                model_pk=candidate.id,
                                provider=provider.name,
                                model=candidate.model_id,
                                reason="OAuth connection unavailable",
                            )
                        continue
                else:
                    api_key = ""
                # 不需要 ApiKey 表也 OK
                # 检查限流：rate_limiter 用 key.id，free/oauth 没有 key — 用 model.id 单独限流不约束
                key_id_for_rate = None
            else:
                # 标准路径：从 ApiKey 表查（优先用 KeyRotator）
                try:
                    from server.core.key_rotator import get_key_rotator
                    picked_pair = await get_key_rotator().pick_key_for_model(session, candidate)
                except Exception:
                    picked_pair = None
                if picked_pair:
                    key_id_for_rate, api_key = picked_pair
                    key = type("K", (), {"id": key_id_for_rate})()  # dummy conforms to .id
                else:
                    result = await session.execute(
                        select(ApiKey)
                        .where(ApiKey.provider_id == candidate.provider_id, ApiKey.is_active == True)
                        .limit(1)
                    )
                    key = result.scalar_one_or_none()
                    if not key or not self.key_manager:
                        if conversation_id:
                            from .route_decision import mark_candidate_skipped
                            mark_candidate_skipped(
                                conversation_id,
                                model_pk=candidate.id,
                                provider=provider.name,
                                model=candidate.model_id,
                                reason="no active API key",
                            )
                        continue
                    key_id_for_rate = key.id
                    api_key = self.key_manager._crypto.decrypt(key.key_encrypted)
                # 检查限流
                can_use = await self.rate_limiter.check_limit(
                    session, candidate.id, key_id_for_rate
                )
                if not can_use:
                    if conversation_id:
                        from .route_decision import mark_candidate_skipped
                        mark_candidate_skipped(
                            conversation_id,
                            model_pk=candidate.id,
                            provider=provider.name,
                            model=candidate.model_id,
                            reason="rate limit exceeded",
                        )
                    continue
            # 防 MissingGreenlet：rate_limiter 内部可能 commit/rollback 导致 provider 对象属性过期，
            # 此处显式刷新需要的属性，避免后续 provider.api_type 触发隐式 lazy-load
            try:
                await session.refresh(provider, attribute_names=["api_type", "credential_type", "name", "base_url", "headers", "oauth_code", "proxy_enabled"])
            except Exception:
                pass  # 对象可能已 detached，后续 getattr 会用缓存值
            # 防 MissingGreenlet：上方 check_limit / 选 key 过程中 rate_limiter 可能 rollback，
            # 使前面已通过顶部守卫的 candidate 再次过期；此处再刷新一次，避免后续 candidate.id
            # 及 RouteResult(model=candidate) 触发隐式 lazy-load 崩溃（与循环顶部守卫对称）。
            try:
                if sa_inspect(candidate).expired:
                    await session.refresh(candidate)
            except Exception:
                try:
                    candidate = await session.merge(candidate)
                except Exception:
                    pass
            adapter = create_adapter_for_provider(provider.api_type)
            provider_name = getattr(provider, "name", str(getattr(provider, "id", "unknown")))
            candidate_name = getattr(candidate, "model_id", str(candidate.id))
            if conversation_id:
                self._set_sticky(conversation_id, candidate.id)
                from .route_decision import mark_selected
                mark_selected(
                    conversation_id,
                    provider=provider_name,
                    model=candidate_name,
                    model_pk=candidate.id,
                    reason="highest ranked available candidate",
                )
            _extra_a = {"__oauth": True} if (getattr(provider, "credential_type", "") == "oauth") else None
            if getattr(provider, "proxy_enabled", False):
                _extra_a = {**(_extra_a or {}), "__proxy_force": True}
            return RouteResult(
                success=True,
                model=candidate,
                provider=provider,
                api_key=api_key,
                key_id=key_id_for_rate,
                adapter=adapter,
                fallback_count=0,
                extra_headers=_extra_a,
                selection_reason="highest ranked available candidate",
            )
        print(f"[AUTO] get_best_candidate exhausted all candidates (rate-limited or no key)")
        return RouteResult(success=False, error="All candidates are rate-limited. Try again later.")
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
