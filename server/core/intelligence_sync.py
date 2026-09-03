"""
智力评分自动同步 v2

主源: LMArena 官方 HuggingFace 数据集 lmarena-ai/leaderboard-dataset
     (text/latest, category=overall) —— 无 key 公开访问（HF datasets-server
     filter API），每日更新，398+ 模型。旧 wulong.dev 镜像仅 20 个模型且频繁 429，已替换。

兜底: OpenRouter /api/v1/models（无 key，424 模型）
     - 桥接匹配：公益站自命名模型 → OpenRouter → 榜单条目
     - 元数据回填：context_length（仍为默认 4096 时）、supports_reasoning_effort

策略: 同步失败绝不清理既有分数（源不可达 ≠ 分数失效）；
     手工校准 (source='manual') 永不覆盖。
"""
import asyncio
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.proxy_pool import get_proxy_pool
from server.db import AsyncSessionLocal
from server.models.intelligence import IntelligenceStatic
from server.models.model import Model

logger = logging.getLogger(__name__)

LMARENA_FILTER_URL = "https://datasets-server.huggingface.co/filter"
LMARENA_PARAMS = {
    "dataset": "lmarena-ai/leaderboard-dataset",
    "config": "text",
    "split": "latest",
    "where": "\"category\"='overall'",
    "limit": 100,
}
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_DATE_SUFFIX_RE = re.compile(r"-(?:20\d{6}|\d{8}|\d{4})$")  # -20260813 / -0813 等 MMDD|YYYYMMDD 形式
_DECOR_RE = re.compile(r"[\[\(（][^\]\)）]*[\]\)）]")
# effort 类变体：匹配时可跨档兼容（分数差异小）；thinking/reasoning 等能力差异不可跨
_EFFORT_TAGS = frozenset({"high", "medium", "low", "xhigh", "max"})
_VARIANT_TAGS = ("xhigh", "thinking", "reasoning", "flash", "pro", "lite",
                 "mini", "nano", "max", "air", "express", "high", "medium", "low")


def _elo_to_intel(elo: float) -> int:
    """ELO -> intelligence 0-100。窗口 1395-1510 → 55-100，尾部低分模型不超 55。"""
    lo, hi = 1395.0, 1510.0
    raw = 55.0 + (float(elo) - lo) / (hi - lo) * 45.0
    return round(max(0, min(100, raw)))


def _norm_name(s: str) -> str:
    """模型名归一化：小写、剥厂商前缀、剥 [xx]/(xx) 装饰、非字母数字转 -、剥日期后缀、剥 -free。"""
    s = (s or "").lower().strip()
    s = s.rsplit("/", 1)[-1]
    s = _DECOR_RE.sub("", s)
    s = re.sub(r"[^a-z0-9._-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = _DATE_SUFFIX_RE.sub("", s)
    if s.endswith("-free"):
        s = s[:-5]
    return s.strip("-")


def _variant_tags(s: str) -> frozenset:
    s = (s or "").lower()
    return frozenset(t for t in _VARIANT_TAGS if ("-" + t) in s or s.endswith(t))


def _effort_compat(a: frozenset, b: frozenset) -> bool:
    """非 effort 标签必须完全一致；effort 标签（high/max/xhigh...）可跨档兼容。"""
    return (a - _EFFORT_TAGS) == (b - _EFFORT_TAGS)


def _match_entry(model_id: str, entries: List[dict]) -> Optional[dict]:
    """aigate 模型名 → 榜单条目。entries 已按 rank 升序。

    tier1: 归一化名精确相等（含剥日期后缀）
    tier2: 前缀兼容 + effort 可跨档（gpt-5.6-sol ↔ gpt-5.6-sol-xhigh），
          多个候选取 rating 最高
    """
    nid = _norm_name(model_id)
    if not nid:
        return None
    ntags = _variant_tags(model_id)
    for e in entries:
        if e["norm"] == nid:
            return e
    best = None
    for e in entries:
        if not _effort_compat(ntags, e["tags"]):
            continue
        prefix_hit = (e["norm"].startswith(nid + "-") or nid.startswith(e["norm"] + "-"))
        if prefix_hit and (best is None or e["rating"] > best["rating"]):
            best = e
    return best


def _bridge_via_openrouter(model_id: str, or_map: Dict[str, dict], lm_entries: List[dict]) -> Optional[dict]:
    """OpenRouter 桥接：aigate 名 → or 条目（归一化精确）→ or 名称归一化 → 榜单前缀匹配。"""
    nid = _norm_name(model_id)
    or_entry = or_map.get(nid)
    if not or_entry:
        return None
    # or 名称归一化后与 nid 常常相同（id 尾段即名称），此时仍用其做榜单前缀匹配
    onid = _norm_name(or_entry["name"]) or nid
    ntags = _variant_tags(model_id)
    best = None
    for e in lm_entries:
        if not _effort_compat(ntags, e["tags"]):
            continue
        prefix_hit = (e["norm"].startswith(onid + "-") or onid.startswith(e["norm"] + "-") or e["norm"] == onid)
        if prefix_hit and (best is None or e["rating"] > best["rating"]):
            best = e
    return best


_last_good_route: Optional[dict] = None  # 进程内记忆上次成功的出网路由，避免每页重复试错


async def _http_get_json(url: str, params: dict = None) -> Optional[dict]:
    """GET JSON，带出网回退链：代理池启用 → 走池；否则直连 → config 代理列表依次回退。
    （HF/OpenRouter 从大陆服务器直连不可达，但海外部署直连即可，池未启用时不应直接放弃。）"""
    global _last_good_route
    from server.config import get_config
    timeout = get_config().arena.timeout_seconds
    pool_kwargs = get_proxy_pool().proxied_kwargs()
    attempts: List[dict] = []
    if pool_kwargs:
        attempts.append(pool_kwargs)
    elif _last_good_route is not None:
        attempts.append(_last_good_route)  # 上次成功路由优先，省掉直连试错
    attempts.append({})
    if not pool_kwargs:
        for p in (get_config().proxy_pool.proxies or []):
            u = (p or {}).get("url")
            if u and {"proxy": u} not in attempts:
                attempts.append({"proxy": u})
    last_err: Optional[Exception] = None
    for kw in attempts:
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, **kw) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                result = resp.json()
                _last_good_route = kw if kw else None
                return result
        except Exception as e:
            last_err = e
            logger.warning("GET %s via %s failed: %s", url, kw.get("proxy") or "direct", e)
    logger.warning("GET %s failed on all %d routes", url, len(attempts))
    return None


async def fetch_lmarena_scores() -> List[dict]:
    """拉取 LMArena 官方数据集 overall 主榜（filter API 分页，每页 100，最多 10 页）。

    返回按 rank 升序的去重条目列表：
    [{name, norm, tags, rating, rank, votes, org}]
    """
    entries: List[dict] = []
    offset = 0
    total = None
    for _page in range(10):
        data = await _http_get_json(LMARENA_FILTER_URL, {**LMARENA_PARAMS, "offset": offset})
        if not data:
            break
        rows = data.get("rows", []) or []
        for r in rows:
            row = r.get("row", {}) or {}
            name = (row.get("model_name") or "").strip()
            rating = row.get("rating")
            if not name or not isinstance(rating, (int, float)):
                continue
            entries.append({
                "name": name,
                "norm": _norm_name(name),
                "tags": _variant_tags(name),
                "rating": float(rating),
                "rank": int(row.get("rank") or 0),
                "votes": int(row.get("vote_count") or 0),
                "org": row.get("organization") or "",
            })
        total = int(data.get("num_rows_total") or 0)
        offset += len(rows)
        if not rows or (total and offset >= total):
            break
    if not entries:
        logger.warning("LMArena leaderboard fetch returned empty")
        return []
    # 归一化撞名时保留 rating 最高的一条（避免前缀匹配命中重复项）
    dedup: Dict[str, dict] = {}
    for e in entries:
        cur = dedup.get(e["norm"])
        if cur is None or e["rating"] > cur["rating"]:
            dedup[e["norm"]] = e
    result = sorted(dedup.values(), key=lambda m: m["rank"] or 9999)
    logger.info("Fetched %d models from LMArena official dataset", len(result))
    return result


async def fetch_openrouter_models() -> Dict[str, dict]:
    """拉取 OpenRouter 模型元数据，返回 {norm_id: {id, name, context_length, supports_reasoning}}。"""
    data = await _http_get_json(OPENROUTER_MODELS_URL)
    if not data:
        return {}
    result: Dict[str, dict] = {}
    for m in (data.get("data") or []):
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        sp = m.get("supported_parameters") or []
        result[_norm_name(mid)] = {
            "id": mid,
            "name": m.get("name") or "",
            "context_length": int(m.get("context_length") or 0),
            "supports_reasoning": any(p in sp for p in ("reasoning_effort", "reasoning")),
        }
    logger.info("Fetched %d models from OpenRouter", len(result))
    return result


async def sync_intelligence(db: AsyncSession) -> int:
    """同步智力评分：LMArena 官方榜 → 匹配 AIGate 模型 → upsert intelligence_static。

    失败不删分：主源拉取失败时直接返回 0，既有分数原样保留。
    返回更新的模型数量。
    """
    entries = await fetch_lmarena_scores()
    if not entries:
        logger.warning("LMArena sync skipped: leaderboard unavailable")
        return 0
    or_map = await fetch_openrouter_models()

    model_rows = (await db.execute(select(Model).where(Model.enabled == True))).scalars().all()

    updated = 0
    metadata_filled = 0
    for m in model_rows:
        # OpenRouter 元数据回填（与分数匹配解耦，归一化精确命中即回填）
        or_entry = or_map.get(_norm_name(m.model_id)) if or_map else None
        if or_entry:
            if getattr(m, "context_length", 4096) == 4096 and or_entry.get("context_length"):
                m.context_length = int(or_entry["context_length"])
                metadata_filled += 1
            if getattr(m, "supports_reasoning_effort", None) is None and or_entry.get("supports_reasoning"):
                m.supports_reasoning_effort = True
                metadata_filled += 1

        matched = _match_entry(m.model_id, entries)
        bridged = False
        if matched is None and or_map:
            matched = _bridge_via_openrouter(m.model_id, or_map, entries)
            bridged = matched is not None
        if matched is None:
            # 未匹配：保留既有分数（如有），绝不清理
            continue

        intel_score = _elo_to_intel(matched["rating"])
        tier = "S" if intel_score >= 85 else "A" if intel_score >= 70 else "B" if intel_score >= 50 else "C"

        existing = (await db.execute(
            select(IntelligenceStatic).where(IntelligenceStatic.pattern == m.model_id)
        )).scalar_one_or_none()

        # 手工校准永不覆盖
        if existing and existing.source == "manual":
            continue
        notes = (f"LMArena rank#{matched['rank']} elo={matched['rating']:.0f} "
                 f"votes={matched['votes']} via={'openrouter' if bridged else 'direct'}")
        if existing:
            existing.score = intel_score
            existing.tier = tier
            existing.source = "arena"
            existing.notes = notes
            existing.updated_at = datetime.utcnow()
        else:
            db.add(IntelligenceStatic(
                pattern=m.model_id,
                score=intel_score,
                tier=tier,
                source="arena",
                notes=notes,
            ))
        updated += 1

    await db.commit()
    logger.info("Intelligence sync complete: %d scored, %d metadata filled", updated, metadata_filled)
    return updated


def start_intelligence_sync() -> Optional[asyncio.Task]:
    """在后台异步执行智力评分同步，避免阻塞网关启动。
    返回一个 asyncio.Task（供关闭时取消），失败时返回 None。"""
    async def _run():
        try:
            async with AsyncSessionLocal() as db:
                n = await sync_intelligence(db)
                if n:
                    logger.info("智力评分已后台同步: %d 个模型", n)
        except Exception as e:
            logger.warning("智力评分后台同步失败: %s", e)

    try:
        asyncio.get_running_loop()   # 无运行中的事件循环时抛 RuntimeError
    except RuntimeError:
        logger.warning("未能启动智力评分后台同步：当前无事件循环")
        return None
    return asyncio.create_task(_run())
