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
    avg_ms: Optional[float] = None
    success_rate: Optional[float] = None
    final_score: float = 0.0
    excluded_reason: Optional[str] = None
    priority_boost: int = 0
    auto_excluded: bool = False
    @property
    def has_full_data(self) -> bool:
        return self.stab_score is not None

# ──────── 智力分估算：模型知名度 / 定价 / 能力 ────────
_CANONICAL_STRIP_PREFIXES = [
    "anthropic/", "openai/", "google/", "xiaomi/", "baidu/", "bytedance/",
    "moonshotai/", "moonshot/", "mistralai/", "deepseek-ai/", "deepseek/",
    "stepfun-ai/", "z-ai/", "zai-org/", "nvidia/", "meta/", "microsoft/",
    "inclusionai/", "sapiens-ai/", "bytedance/",
]
_CANONICAL_STRIP_SUFFIXES = [
    "-20260210", "-20250929", "-20251001", "-20250805", "-20250514",
    "-0309", "-preview", "-online", "-web", "-console", "-search",
    "-deep-research", "-edit", "-latest",
    "-32k", "-16k", "-8k", "-1m",
    "-minimal",
    # NOTE: -high/-medium/-low/-xhigh/-compact are EFFORT TIERS, not portals;
    # do not strip them ? they affect intelligence.
    "-nano", "-mini", "-lite", "-flash", "-turbo", "-air", "-express",
]

def canonicalize(model_id: str) -> str:
    """Strip vendor prefixes / date suffixes / size tags / portal variants so
    model-identity matching hits the correct intelligence_static pattern."""
    s0 = model_id.strip().lower()
    for p in _CANONICAL_STRIP_PREFIXES:
        if s0.startswith(p):
            s0 = s0[len(p):]
            break
    while True:
        changed = False
        for sfx in _CANONICAL_STRIP_SUFFIXES:
            if s0.endswith(sfx):
                s0 = s0[:-len(sfx)]
                changed = True
        if not changed:
            break
    s0 = s0.replace("-thinking", "").replace("-reasoning", "").replace("_", "-")
    return s0.strip("-")


_ARENA_ELO_LO, _ARENA_ELO_HI = 1400.0, 1510.0
def _elo_to_intel_wide(elo: float) -> float:
    """Arena ELO -> 55..100 wide spread, so close ranks actually differ."""
    raw = 55.0 + (float(elo) - _ARENA_ELO_LO) / (_ARENA_ELO_HI - _ARENA_ELO_LO) * 45.0
    return round(max(0.0, min(100.0, raw)), 2)


def _resolve_intel_from_row(r) -> tuple:
    """Read raw score from intelligence_static row; if it carries Arena ELO in
    notes, re-map ELO to the wide 55-100 spread for better discrimination.
    Returns (score, source).
    """
    src = "Arena" if (r.notes and "Arena" in r.notes) else "Manual"
    if src == "Arena" and r.notes:
        import re as _re
        m = _re.search(r"score=(\d+(?:\.\d+)?)", r.notes)
        if m:
            try:
                elo = float(m.group(1))
                return _elo_to_intel_wide(elo), "Arena"
            except Exception:
                pass
    return float(r.score), src


_DEMOTE_KEYWORDS = [
    ("flash", -15), ("lite", -15), ("mini", -15), ("small", -15),
    ("nano", -15), ("scout", -20), ("haiku", -10),
    ("embedding", -30), ("moderation", -30), ("tts", -30),
    ("llada", -10), ("ling-", -10),
]

_DEMOTE_PARAM = [
    ("-17b", -20), ("-7b", -20), ("-3b", -20), ("-4b", -25),
    ("-14b", -15), ("-8b", -15),     ("-1b", -30), ("-0b", -30),
]

# 家族基线：无 Arena/手工评分时，按「已知强模型家族」给合理起始分，
# 避免所有未匹配模型都挤在 ~58（那样大模型和小模型评分几乎无差别，显得不准）。
# cap 92：不会超过 Arena 顶级（95-100），也不至于把免费小模型抬到顶流位置。
_FAMILY_BASE = {
    "claude-fable": 95, "claude-opus-4-8": 94, "claude-opus-4-7": 93,
    "claude-opus-4-6": 92, "claude-opus-4-5": 91, "claude-opus-4": 90,
    "claude-sonnet-5": 90, "claude-sonnet-4": 88, "claude-haiku": 80,
    "gpt-5.6": 93, "gpt-5.5": 92, "gpt-5.4": 91, "gpt-5.2": 90,
    "gpt-5.1": 89, "gpt-5": 88, "gpt-4.1": 87, "gpt-4o": 80,
    "o3": 90, "o1": 88,
    "gemini-3.5": 90, "gemini-3.1": 89, "gemini-3": 88, "gemini-2.5": 84,
    "gemini-2.0": 80, "gemini-1.5": 75,
    "deepseek-v4": 88, "deepseek-v3": 82, "deepseek-r": 84, "deepseek-chat": 78,
    "grok-4.20": 90, "grok-4.1": 88, "grok-3": 82,
    "qwen3.7": 86, "qwen3.6": 85, "qwen3.5": 84, "qwen3": 82, "qwen2.5": 78,
    "glm-5": 84, "glm-4": 76,
    "ernie-5": 84, "ernie-4": 78,
    "doubao-seed-2.1": 80, "doubao-seed-2.0": 76,
    "mistral-large": 76, "mistral": 72,
    "llama-3.3": 75, "llama-3.1": 72, "llama-3": 70,
    "phi-4": 70,
}

def estimate_intel(model_id: str, is_free: bool) -> float:
    """Conservative fallback when no intelligence_static / Arena match.
    Starts low and only applies reliable demotion/effort adjustments;
    never inflates above 62 (real strong models should have Arena data).
    """
    low = model_id.lower()
    base = 58.0
    # 家族基线：取命中的最强家族分（已知强模型给合理起始分，而非一律 58）
    for fam, fb in _FAMILY_BASE.items():
        if fam in low:
            base = max(base, fb)
    # effort tier handling ? small +/- per tier
    if "-xhigh" in low or "-reasoning" in low or "-reason" in low or "-console" in low or "-thinking" in low:
        base += 8
    elif "-high" in low:
        base += 6
    elif "-medium" in low or "-standard" in low:
        pass
    elif "-low" in low or "-basic" in low or "-express" in low or "-air" in low:
        base -= 6
    # demote by capability keyword (only the strongest demotion)
    for kw, penalty in _DEMOTE_KEYWORDS:
        if kw in low:
            base += penalty
            break
    # demote by parameter size
    for kw, penalty in _DEMOTE_PARAM:
        if kw in low:
            base += penalty
            break
    return min(92.0, max(20.0, base))
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
                    s, src = _resolve_intel_from_row(r)
                    if best_score is None or s > best_score:
                        best_score = s
                        best_source = src
            except Exception:
                continue
        # canonical fallback: strip vendor/date/size/portal variants so misspelt id matches
        if best_score is None:
            canon = canonicalize(model_id)
            for r in intel_rows:
                try:
                    canon_pattern = canonicalize(r.pattern)
                    if canon == canon_pattern or self._like_match(canon, r.pattern.replace("*", "%").replace("?", "_").lower()):
                        s, src = _resolve_intel_from_row(r)
                        if best_score is None or s > best_score:
                            best_score = s
                            best_source = src + "-canonical"
                except Exception:
                    continue
        if best_score is not None:
            return float(best_score), best_source or "Manual"
        return estimate_intel(model_id, is_free), "Estimate"
    @staticmethod
    def _like_match(s: str, pattern: str) -> bool:
        import re
        regex = "^" + re.escape(pattern).replace("%", ".*").replace("_", ".") + "$"
        return bool(re.match(regex, s))
    async def compute_speed_score(self, db: AsyncSession, model_id: int, model_id_str: str = None) -> tuple:
        """返回 (speed_score 0-100, avg_ms or None)
        仅取历史「成功调用」(request_logs.status='success') 的延迟求均值；
        映射曲线高延迟也永不为 0：score = max(FLOOR, 100*REF/(REF+avg))，REF≈3000ms 对应 50 分。"""
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
            )
        )).fetchall()
        latencies = [r[0] for r in rows if r[0] is not None]
        if not latencies:
            return 0.0, None
        avg = sum(latencies) / len(latencies)
        REF = 3000.0   # 参考延迟：3000ms → 50 分
        FLOOR = 5.0    # 高延迟也不低于此分（永不为 0）
        score = 100.0 * REF / (REF + avg)
        score = max(FLOOR, min(100.0, score))
        return round(score, 2), round(avg, 1)
    async def compute_stab_score(self, db: AsyncSession, model_id: int, model_id_str: str = None) -> Optional[float]:
        """返回 stab_score (0-100) 或 None(样本不足)。
        仅基于 request_logs 真实调用历史计算成功率（不依赖已停用的自动探测）。"""
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
        all_statuses = [r[0] for r in rows]
        if not all_statuses:
            return 70.0  # baseline stability; unknown models enter Auto
        total = len(all_statuses)
        success = sum(1 for r0 in all_statuses if r0 == "success" or r0 == "healthy")
        sr = success / total
        consecutive_fail = 0
        for r0 in all_statuses:
            if r0 != "success":
                consecutive_fail += 1
            else:
                break
        penalty = 0
        if consecutive_fail >= 3:
            penalty = self.fail_penalty + max(0, consecutive_fail - 3) * 2
        if total < self.min_requests:
            penalty += (self.min_requests - total) * 10
        return round(max(0.0, sr * 100 - penalty), 2)
    async def compute_model_health(
        self, db: AsyncSession, model_id: int, model_id_str: str = None,
        window_seconds: int = 86400,
    ) -> dict:
        """从 request_logs 真实调用历史推导单模型健康状态（替代已停用的自动探测）。

        返回 {status, latency_ms, last_checked, error_message}
        status ∈ healthy / degraded / rate_limited / unhealthy / unknown
        """
        since = datetime.utcnow() - timedelta(seconds=window_seconds)
        model = await db.get(Model, model_id)
        needle = (model.model_id if model else None) or model_id_str
        if not needle:
            return {"status": "unknown", "latency_ms": None, "last_checked": None, "error_message": None}
        rows = (await db.execute(
            select(
                RequestLog.status, RequestLog.latency_ms, RequestLog.created_at,
                RequestLog.error_type, RequestLog.http_status,
            ).where(
                RequestLog.created_at >= since,
                RequestLog.routed_model == needle,
            ).order_by(RequestLog.created_at.desc()).limit(200)
        )).fetchall()
        if not rows:
            return {"status": "unknown", "latency_ms": None, "last_checked": None, "error_message": None}
        total = len(rows)
        success = sum(1 for r in rows if r[0] == "success")
        latencies = [r[1] for r in rows if r[1] is not None]
        avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None
        last = rows[0]  # 最新一条（已按时间倒序）
        last_status, last_err_type, last_http = last[0], last[3], last[4]
        last_checked = last[2].isoformat() if last[2] else None
        sr = success / total
        if last_status != "success":
            if last_http == 429 or (last_err_type and "rate" in str(last_err_type).lower()):
                status = "rate_limited"
            else:
                status = "unhealthy"
        elif sr < 0.5:
            status = "unhealthy"
        elif sr < 0.9:
            status = "degraded"
        else:
            status = "healthy"
        err_msg = None
        if status != "healthy":
            err_msg = (last_err_type or (f"近期成功率 {sr*100:.0f}%"))
        return {
            "status": status,
            "latency_ms": avg_latency,
            "last_checked": last_checked,
            "error_message": err_msg,
        }
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
            ms.speed_score, ms.avg_ms = await self.compute_speed_score(db, m.id, m.model_id)
            ms.intel_score, ms.intel_source = self.compute_intel(m.model_id, list(intel_rows), m.is_free)
            # manual boost lifts the effective intel tier (not final_score directly)
            if m.priority_boost:
                ms.intel_score = round(min(100.0, ms.intel_score + m.priority_boost * 0.3), 2)
                ms.intel_source = (ms.intel_source or "") + "-boosted"
            ms.stab_score = await self.compute_stab_score(db, m.id, m.model_id)
            # stability is never None now (70 default), so models enter Auto
            speed_norm = min(1.0, max(0.0, (ms.speed_score or 0.0) / 100.0))
            stab_norm = min(1.0, max(0.0, (ms.stab_score or 70.0) / 100.0))
            intel_eff = ms.intel_score
            # 综合分：智力主导 + 速度/稳定性增量贡献。
            # 原实现用「intel<65 硬阈值」把弱模型总分压到 <10 并在 65 处制造 60 分断崖，
            # 且弱模型之间（intel 43~64）分数几乎无区分。现统一公式：
            # 弱模型拿到其 intel 量级分数（保留区分度），强模型仍稳居前排，选举排序不变。
            ms.final_score = round(
                intel_eff
                + (100.0 - intel_eff) * 0.25 * speed_norm
                + stab_norm * 4.0
            , 2)
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
                    "model": m.model_id, "avg_ms": p50, "speed_score": speed,
                })
        results.sort(key=lambda x: x["avg_ms"])
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
