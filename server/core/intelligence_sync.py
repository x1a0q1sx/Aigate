"""
智力评分自动同步
启动时从 LMSys Arena AI Leaderboard 拉取最新 ELO 排名，映射为 0-100 智力分
"""
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from server.core.proxy_pool import get_proxy_pool
from server.db import AsyncSessionLocal
from server.models.intelligence import IntelligenceStatic
from server.models.model import Model

logger = logging.getLogger(__name__)

ARENA_API = "https://api.wulong.dev/arena-ai-leaderboards/v1/leaderboard?name=text"


def _elo_to_intel(elo: float) -> int:
    """ELO -> intelligence 0-100. Wide spread, close Arena ranks differ.

    Arena leaderboard ELO clusters in ~1400-1510; map that window to 55-100,
    clamping outliers so a low-ELO model never exceeds 55 and top models cap at 100.
    """
    lo, hi = 1400.0, 1510.0
    raw = 55.0 + (float(elo) - lo) / (hi - lo) * 45.0
    return round(max(0, min(100, raw)))


def _model_name_match(aigate_model_id: str, arena_model_name: str) -> bool:
    """严格模糊匹配：避免跨变体（thinking/flash/pro/...）错配。

    匹配顺序：
      1) 精确（含变体后缀）   claude-opus-4-6 ↔ claude-opus-4-6
      2) 去厂商前缀后相等      anthropic/claude-opus-4-6 ↔ claude-opus-4-6
      3) 仅在「变体标签集合完全一致」时才允许子串包含，
         杜绝 claude-opus-4-6(无 thinking) 错配到 ...-thinking 的分数
    """
    VARIANT_TAGS = ("thinking", "reasoning", "flash", "pro", "lite",
                    "mini", "nano", "max", "air", "express", "high", "medium", "low")

    def variant_tags(s: str) -> frozenset:
        s = s.lower()
        return frozenset(t for t in VARIANT_TAGS if ("-" + t) in s or s.endswith(t))

    def norm(s: str) -> str:
        s = s.lower().strip()
        return s.rsplit("/", 1)[-1] if "/" in s else s

    a, b = norm(aigate_model_id), norm(arena_model_name)
    if a == b:
        return True
    # 变体标签不一致 → 绝不跨变体匹配
    if variant_tags(aigate_model_id) != variant_tags(arena_model_name):
        return False
    # 标签一致时才允许短名子串包含
    return a in b or b in a


async def fetch_arena_scores() -> Dict[str, dict]:
    """拉取 Arena AI 排行榜，返回 {模型名: {score, rank, vendor, votes}}。
    通过代理池出公网（网关通常需 socks5 才能访问外网），代理关闭时回退直连。"""
    from server.config import get_config
    timeout = get_config().arena.timeout_seconds
    proxy_kwargs = get_proxy_pool().proxied_kwargs()   # 启用时 {"proxy": "socks5://..."}, 否则 {}
    try:
        async with httpx.AsyncClient(timeout=timeout, **proxy_kwargs) as client:
            resp = await client.get(ARENA_API)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Failed to fetch Arena AI leaderboard: %s", e)
        return {}

    models = data.get("models", [])
    result = {}
    for entry in models:
        name = entry.get("model", "").strip()
        if not name:
            continue
        result[name] = {
            "score": entry.get("score", 0),
            "rank": entry.get("rank", 0),
            "vendor": entry.get("vendor", ""),
            "votes": entry.get("votes", 0),
        }
    logger.info("Fetched %d models from Arena AI leaderboard", len(result))
    return result


async def sync_intelligence(db: AsyncSession) -> int:
    """
    同步智力评分：拉取 Arena 排行榜 → 匹配 AIGate 模型 → 写入 intelligence_static
    返回更新的模型数量
    """
    scores = await fetch_arena_scores()
    if not scores:
        return 0

    # 查找所有已启用模型
    model_rows = (await db.execute(select(Model).where(Model.enabled == True))).scalars().all()

    updated = 0
    matched_model_ids = set()
    for m in model_rows:
        matched = None
        # 精确匹配
        if m.model_id in scores:
            matched = scores[m.model_id]
        else:
            # 模糊匹配
            for arena_name, info in scores.items():
                if _model_name_match(m.model_id, arena_name):
                    matched = info
                    break

        if not matched:
            continue
        matched_model_ids.add(m.model_id)

        intel_score = _elo_to_intel(matched["score"])
        tier = "S" if intel_score >= 85 else "A" if intel_score >= 70 else "B" if intel_score >= 50 else "C"

        # upsert into intelligence_static
        existing = (await db.execute(
            select(IntelligenceStatic).where(IntelligenceStatic.pattern == m.model_id)
        )).scalar_one_or_none()

        # 非破坏性：手工校准(source='manual') 永不覆盖
        if existing and existing.source == "manual":
            continue
        if existing:
            existing.score = intel_score
            existing.tier = tier
            existing.source = "arena"
            existing.notes = f"Arena rank#{matched['rank']} score={matched['score']} votes={matched['votes']}"
            existing.updated_at = datetime.utcnow()
        else:
            db.add(IntelligenceStatic(
                pattern=m.model_id,
                score=intel_score,
                tier=tier,
                source="arena",
                notes=f"Arena rank#{matched['rank']} score={matched['score']} votes={matched['votes']}",
            ))
        updated += 1

    # 清理：已启用模型中「未匹配到 Arena」且来源为 arena 的过期行
    # → 删除后改走 estimate_intel（家族基线），避免残留的松散错配分数
    cleaned = 0
    for m in model_rows:
        if m.model_id in matched_model_ids:
            continue
        stale = (await db.execute(
            select(IntelligenceStatic).where(
                IntelligenceStatic.pattern == m.model_id,
                IntelligenceStatic.source == "arena",
            )
        )).scalar_one_or_none()
        if stale:
            await db.delete(stale)
            cleaned += 1

    await db.commit()
    logger.info("Intelligence sync complete: %d updated, %d stale cleaned", updated, cleaned)
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
