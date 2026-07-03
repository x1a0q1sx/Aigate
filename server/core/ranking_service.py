"""
综合打分服务
v0.3: speed + intelligence + stability 三维度加权，智力分支持 Arena ELO / 启发式估算
"""
import time
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, desc, and_
from server.models.model import Model
from server.models.provider import Provider
from server.models.request_log import RequestLog
from server.models.intelligence import IntelligenceStatic
from server.models.routing_config import RoutingWeights, RoutingPin
from server.models.health_check import HealthCheck
@dataclass
class ModelScore:
    model_id: int
    provider_name: str
    model_id_str: str
    display_name: str
    is_free: bool
    speed_score: float = 0.0
    intel_score: float = 50.0
    intel_source: str = "估算"           # Arena / 手动 / 估算
    stab_score: Optional[float] = None
    p50_ms: Optional[int] = None
    success_rate: Optional[float] = None
    final_score: float = 0.0
    excluded_reason: Optional[str] = None
    priority_boost: int = 0
    auto_excluded: bool = False
    @property
    def has_full_data(self) -> bool:
        return self.stab_score is not None

# ──────── 智力分估算：模型知名度 / 定价 / 能力 ────────
_TIER_HIGH = {"gpt-5", "gpt-5.4", "gpt-5.5", "claude-opus", "claude-fable",
              "claude-sonnet-4-6", "gemini-3", "gemini-3.5", "glm-5", "qwen3",
              "deepseek-v4", "kimi-k2", "mimo-v2.5-pro", "minimax-m3",
              "grok-4.20", "step-3", "muse-spark"}
_TIER_MID = {"deepseek-v3", "deepseek-v4-flash", "qwen2.5", "claude-sonnet-4-5",
             "gpt-4o", "gemini-2.5", "minimax-m2", "kimi-k1", "deepseek-r1",
             "step-2", "glm-4", "qwen3-0", "grok-3", "mistral-large"}
_KNOWN_INTEL = {}  # {partial_name: int(score)}
for k in _TIER_HIGH:
    _KNOWN_INTEL[k] = 85
for k in _TIER_MID:
    _KNOWN_INTEL[k] = 70

def estimate_intel(model_id: str, is_free: bool) -> float:
    """未匹配 Arena 时，基于模型知名度和定价估算智力分"""
    low = model_id.lower()
    for prefix, score in _KNOWN_INTEL.items():
        if prefix in low:
            return float(score)
    # 免费模型 + 5 基础分
    base = 55.0 if is_free else 50.0
    # 按参数规模微调
    if "pro" in low or "max" in low or "large" in low:
        base += 8
    if "flash" in low or "lite" in low or "mini" in low or "small" in low:
        base -= 5
    if "thinking" in low or "reason" in low:
        base += 3
    return base


class RankingService:
    """综合打分服务（无状态，每次新建）"""
    def __init__(self, speed_window_seconds: int = 86400,
                 stab_window_seconds: int = 86400,
                 min_requests: int = 2,
                 fail_penalty: int = 10):
        self.speed_window_seconds = speed_window_seconds
        self.stab_window_seconds = stab_window_seconds
        self.min_requests = min_requests
        self.fail_penalty = fail_penalty
    async def get_weights(self, db: AsyncSession) -> Dict[str, float]:
        row = (await db.execute(select(RoutingWeights).where(RoutingWeights.id == 1))).scalar_one_or_none()
        if not row:
            return {"w_speed": 0.30, "w_intel": 0.50, "w_stab": 0.20}
        return {
            "w_speed": float(row.w_speed),
            "w_intel": float(row.w_intel),
            "w_stab": float(row.w_stab),
        }
    async def get_pinned_model_id(self, db: AsyncSession) -> Optional[int]:
        row = (await db.execute(select(RoutingPin).where(RoutingPin.id == 1))).scalar_one_or_none()
        return row.pinned_model_id if row else None
    def compute_intel(self, model_id: str, intel_rows: List[IntelligenceStatic],
                       is_free: bool = False) -> tuple:
        """
        计算智力分，返回 (score, source)
        1) 优先 intelligence_static 手动配置（识别标记 source=手动）
        2) 其次 Arena 同步的数据（source=Arena）
        3) 兜底估算（source=估算）
        """
        best_score = None
        best_source = None
        for r in intel_rows:
            pat = r.pattern.replace("*", "%").replace("?", "_")
            try:
                if self._like_match(model_id.lower(), pat.lower()):
                    s = r.score
                    src = "Arena" if r.notes and "Arena" in r.notes else "手动"
                    if best_score is None or s > best_score:
                        best_score = s
                        best_source = src
            except Exception:
                continue
        if best_score is not None:
            return float(best_score), best_source or "手动"
        return estimate_intel(model_id, is_free), "估算"
    @staticmethod
    def _like_match(s: str, pattern: str) -> bool:
        import re
        regex = "^" + re.escape(pattern).replace("%", ".*").replace("_", ".") + "$"
        return bool(re.match(regex, s))
    async def compute_speed_score(self, db: AsyncSession, model_id: int, model_id_str: str = None) -> tuple:
        """返回 (speed_score 0-100, p50_ms or None)
        优先生从 request_logs 取 success 记录的延迟，其次从 health_check 表取最近一次探测延迟"""
        since = datetime.utcnow() - timedelta(seconds=self.speed_window_seconds)
        model = await db.get(Model, model_id)
        needle = (model.model_id if model else None) or model_id_str
        if not needle:
            return 0.0, None
        rows = (await db.execute(
            select(RequestLog.latency_ms).where(
                RequestLog.created_at >= since,
                RequestLog.status == "success",
                RequestLog.latency_ms.isnot(None),
                RequestLog.routed_model == needle,
            ).order_by(RequestLog.latency_ms)
        )).fetchall()
        latencies = [r[0] for r in rows if r[0] is not None]
        if not latencies:
            # fallback: 从健康探测表取延迟（哪怕状态是 degraded/rate_limited 也算）
            hc_rows = (await db.execute(
                select(HealthCheck.latency_ms).where(
                    HealthCheck.model_id == model_id,
                    HealthCheck.latency_ms.isnot(None),
                    HealthCheck.checked_at >= since,
                ).order_by(HealthCheck.checked_at.desc()).limit(5)
            )).fetchall()
            latencies = [r[0] for r in hc_rows if r[0] is not None and r[0] > 0]
        if not latencies:
            return 0.0, None
        p50 = latencies[len(latencies) // 2]
        speed = max(0.0, 100.0 - min(p50, 5000) / 50.0)
        return round(speed, 2), int(p50)
    async def compute_stab_score(self, db: AsyncSession, model_id: int, model_id_str: str = None) -> Optional[float]:
        """返回 stab_score (0-100) 或 None(样本不足)。
        同时从 request_logs 和 health_check 取数据，优先 request_logs 作为主要判断依据"""
        since = datetime.utcnow() - timedelta(seconds=self.stab_window_seconds)
        model = await db.get(Model, model_id)
        needle = (model.model_id if model else None) or model_id_str
        if not needle:
            return None
        rows = (await db.execute(
            select(RequestLog.status).where(
                RequestLog.created_at >= since,
                RequestLog.routed_model == needle,
            ).order_by(RequestLog.created_at.desc()).limit(200)
        )).fetchall()
        # 也查健康探测记录
        hc_rows = (await db.execute(
            select(HealthCheck.status).where(
                HealthCheck.model_id == model_id,
                HealthCheck.checked_at >= since,
            ).order_by(HealthCheck.checked_at.desc()).limit(200)
        )).fetchall()
        all_statuses = [r[0] for r in rows] + [r[0] for r in hc_rows]
        if not all_statuses:
            return None
        total = len(all_statuses)
        success = sum(1 for s in all_statuses if s == "success" or s == "healthy")
        sr = success / total
        # 连续失败惩罚（只看 request_logs 的连续失败）
        consecutive_fail = 0
        for r in rows:
            if r[0] != "success":
                consecutive_fail += 1
            else:
                break
        penalty = 0
        if consecutive_fail >= 3:
            penalty = self.fail_penalty + max(0, consecutive_fail - 3) * 2
        # 样本不足惩罚：少于 min_requests 时降权
        if total < self.min_requests:
            penalty += (self.min_requests - total) * 15
        return round(max(0.0, sr * 100 - penalty), 2)
    async def rank_all(
        self, db: AsyncSession,
        models: List[Model],
        providers: Dict[int, Provider],
        health_cooling: Dict[int, datetime] = None,
    ) -> List[ModelScore]:
        """对所有候选模型打分排序"""
        weights = await self.get_weights(db)
        intel_rows = (await db.execute(select(IntelligenceStatic))).scalars().all()
        scores: List[ModelScore] = []
        health_cooling = health_cooling or {}
        now = datetime.utcnow()
        for m in models:
            prov = providers.get(m.provider_id)
            if not prov:
                continue
            ms = ModelScore(
                model_id=m.id,
                provider_name=prov.name,
                model_id_str=m.model_id,
                display_name=m.display_name or m.model_id,
                is_free=m.is_free,
                priority_boost=m.priority_boost or 0,
                auto_excluded=m.auto_excluded or False,
            )
            # 过滤：启用 + 参与 auto
            if not m.enabled or not m.auto_enabled:
                ms.excluded_reason = "未启用/不参与 auto"
                scores.append(ms); continue
            if m.auto_excluded:
                ms.excluded_reason = "已强制排除"
                scores.append(ms); continue
            # 手动冷却
            if m.manual_cooldown_until and m.manual_cooldown_until > now:
                ms.excluded_reason = f"手动冷却至 {m.manual_cooldown_until.isoformat()[:19]}"
                scores.append(ms); continue
            # 健康冷却
            if m.id in health_cooling and health_cooling[m.id] > now:
                ms.excluded_reason = "健康冷却中"
                scores.append(ms); continue
            # 计算三维
            ms.speed_score, ms.p50_ms = await self.compute_speed_score(db, m.id, m.model_id)
            ms.intel_score, ms.intel_source = self.compute_intel(m.model_id, list(intel_rows), m.is_free)
            ms.stab_score = await self.compute_stab_score(db, m.id, m.model_id)
            # 稳定性不足 → 排除
            if ms.stab_score is None:
                ms.excluded_reason = "稳定性样本不足"
                scores.append(ms); continue
            # final_score
            ms.final_score = round(
                weights["w_speed"] * ms.speed_score
                + weights["w_intel"] * ms.intel_score
                + weights["w_stab"] * ms.stab_score
                , 2)
            # priority_boost 加成（每 +1 加 0.5 分）
            ms.final_score += m.priority_boost * 0.5
            scores.append(ms)
        # 排序：排除的排后面；final_score 降序；tie-breaker free 优先
        def sort_key(s: ModelScore):
            excluded = 1 if s.excluded_reason else 0
            return (
                excluded,
                -s.final_score if s.has_full_data else 1,
                0 if s.is_free else 1,
            )
        scores.sort(key=sort_key)
        return scores
    async def rank_top_speed(self, db: AsyncSession, limit: int = 5) -> List[Dict[str, Any]]:
        models = (await db.execute(select(Model, Provider)
                .join(Provider, Model.provider_id == Provider.id)
                .where(Model.enabled == True, Model.auto_enabled == True)
                )).all()
        providers = {m.provider_id: p for m, p in models}
        results = []
        for m, p in models:
            speed, p50 = await self.compute_speed_score(db, m.id, m.model_id)
            if p50 is not None:
                results.append({
                    "model_id": m.id, "provider": p.name,
                    "model": m.model_id, "p50_ms": p50, "speed_score": speed,
                })
        results.sort(key=lambda x: x["p50_ms"])
        return results[:limit]
    async def rank_top_intel(self, db: AsyncSession, limit: int = 10) -> List[Dict[str, Any]]:
        models = (await db.execute(select(Model, Provider)
                .join(Provider, Model.provider_id == Provider.id)
                .where(Model.enabled == True)
                )).all()
        intel_rows = (await db.execute(select(IntelligenceStatic))).scalars().all()
        results = []
        for m, p in models:
            score = self.match_intel(m.model_id, list(intel_rows))
            tier = "C"
            for r in intel_rows:
                if self._like_match(m.model_id.lower(), r.pattern.lower().replace("*", "%")):
                    tier = r.tier; break
            results.append({
                "model_id": m.id, "provider": p.name,
                "model": m.model_id, "score": score, "tier": tier,
            })
        results.sort(key=lambda x: -x["score"])
        return results[:limit]
    async def rank_top_stab(self, db: AsyncSession, limit: int = 10) -> List[Dict[str, Any]]:
        models = (await db.execute(select(Model, Provider)
                .join(Provider, Model.provider_id == Provider.id)
                .where(Model.enabled == True, Model.auto_enabled == True)
                )).all()
        results = []
        for m, p in models:
            sr = await self.compute_stab_score(db, m.id, m.model_id)
            results.append({
                "model_id": m.id, "provider": p.name,
                "model": m.model_id,
                "stability_score": sr,
                "sample_status": "充足" if sr is not None else "样本不足",
            })
        results.sort(key=lambda x: -(x["stability_score"] or -1))
        return results[:limit]
