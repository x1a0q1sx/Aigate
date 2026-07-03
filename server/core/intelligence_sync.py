"""
智力评分自动同步
启动时从 LMSys Arena AI Leaderboard 拉取最新 ELO 排名，映射为 0-100 智力分
"""
import logging
from datetime import datetime
from typing import Dict, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from server.models.intelligence import IntelligenceStatic
from server.models.model import Model

logger = logging.getLogger(__name__)

ARENA_API = "https://api.wulong.dev/arena-ai-leaderboards/v1/leaderboard?name=text"


def _elo_to_intel(elo: float) -> int:
    """ELO (600~1600) → 智力分 (0-100)，更宽的分布"""
    raw = round((elo - 600) / 10)
    return max(0, min(100, raw))


def _model_name_match(aigate_model_id: str, arena_model_name: str) -> bool:
    """模糊匹配：双向 contains + 去前缀/变体"""
    a = aigate_model_id.lower().replace("-thinking", "").replace("-online", "")
    b = arena_model_name.lower().replace("-thinking", "").replace("-online", "")
    if a == b:
        return True
    # deepseek-ai/deepseek-v4-flash ↔ deepseek-v4-flash
    if "/" in a:
        a_short = a.rsplit("/", 1)[-1]
        if a_short == b:
            return True
    if "/" in b:
        b_short = b.rsplit("/", 1)[-1]
        if a == b_short:
            return True
    return a in b or b in a


async def fetch_arena_scores() -> Dict[str, dict]:
    """拉取 Arena AI 排行榜，返回 {模型名: {score, rank, vendor, votes}}"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
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

        intel_score = _elo_to_intel(matched["score"])
        tier = "S" if intel_score >= 85 else "A" if intel_score >= 70 else "B" if intel_score >= 50 else "C"

        # upsert into intelligence_static
        existing = (await db.execute(
            select(IntelligenceStatic).where(IntelligenceStatic.pattern == m.model_id)
        )).scalar_one_or_none()

        if existing:
            existing.score = intel_score
            existing.tier = tier
            existing.notes = f"Arena rank#{matched['rank']} score={matched['score']} votes={matched['votes']}"
            existing.updated_at = datetime.utcnow()
        else:
            db.add(IntelligenceStatic(
                pattern=m.model_id,
                score=intel_score,
                tier=tier,
                notes=f"Arena rank#{matched['rank']} score={matched['score']} votes={matched['votes']}",
            ))
        updated += 1

    # 也把匹配到的但不在 intelligence_static 里的 arena 模型写入
    existing_patterns = set(
        (r[0] for r in (await db.execute(select(IntelligenceStatic.pattern))).fetchall())
    )
    for arena_name, info in scores.items():
        if arena_name in existing_patterns:
            continue
        intel_score = _elo_to_intel(info["score"])
        tier = "S" if intel_score >= 85 else "A" if intel_score >= 70 else "B" if intel_score >= 50 else "C"
        db.add(IntelligenceStatic(
            pattern=arena_name,
            score=intel_score,
            tier=tier,
            notes=f"Arena rank#{info['rank']} score={info['score']} votes={info['votes']}",
        ))
        updated += 1

    await db.commit()
    logger.info("Intelligence sync complete: %d models updated", updated)
    return updated
