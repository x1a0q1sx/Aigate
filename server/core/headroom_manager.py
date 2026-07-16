"""
Headroom Manager（P3-1）
思路来自 9Router Headroom：用户可在某些 provider 上保留 "弹性空间"
对一个 provider 子集保留额度不入参自动选择，避免 quota 用尽时不留余地。
此模块只是配置 + 查询入口，实际在路由侧（auto_router）跳过未达耗用阈值的 headroom 候选。

存储：config.headroom.entries:
  [
    {"provider_id": 1, "daily_token_limit": 50000, "label": "保留备用"},
    ...
  ]
查询：
  get_headroom_for(provider_id) → int token_limit (0 表示不限制)
  is_in_headroom_cooling(provider_id, db) → bool
    通过 request_logs 表（routed_provider_id）当日累积 + 配置阈值判断
"""
from __future__ import annotations
from typing import List, Dict, Optional
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from server.config import get_config
from server.models.request_log import RequestLog

config = get_config()


def get_headroom_entries() -> List[Dict]:
    """读取配置文件中所有的 headroom 条目"""
    return list(getattr(config, "headroom", {}).get("entries", []) if isinstance(getattr(config, "headroom", None), dict)
                else getattr(config.headroom, "entries", []))


def get_headroom_for(provider_id: int) -> int:
    """返回该 provider 的每日 token 上限，0 表示不限"""
    for entry in get_headroom_entries():
        if entry.get("provider_id") == provider_id:
            return int(entry.get("daily_token_limit", 0))
    return 0


async def get_provider_breakdown(db: AsyncSession) -> List[Dict]:
    """按 provider 拆分今日 token 消耗（供 headroom 与前端使用）。

    方案A：数据源从 quota_usage 改为 request_logs（routed_provider_id + token 列）。
    """
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (await db.execute(
        select(
            RequestLog.routed_provider_id,
            func.coalesce(func.sum(RequestLog.prompt_tokens + RequestLog.completion_tokens), 0),
        ).where(
            RequestLog.created_at >= today_start,
            RequestLog.routed_provider_id.isnot(None),
        ).group_by(RequestLog.routed_provider_id)
    )).all()
    return [{"provider_id": r[0], "tokens": int(r[1] or 0)} for r in rows]


async def is_in_headroom_cooling(provider_id: int, db: AsyncSession) -> bool:
    """如果该 provider 今日累计 token 已 ≥ headroom 阈值 → True（应被路由跳过）"""
    limit = get_headroom_for(provider_id)
    if limit <= 0:
        return False
    breakdown = await get_provider_breakdown(db)
    for row in breakdown:
        if row.get("provider_id") == provider_id:
            return int(row.get("tokens", 0)) >= limit
    return False


async def get_headroom_status(db: AsyncSession) -> List[Dict]:
    """供前台展示：每个保留 threshold + 今日已用"""
    out = []
    entries = get_headroom_entries()
    breakdown = await get_provider_breakdown(db)
    by_pid = {row["provider_id"]: row for row in breakdown}
    from server.models.provider import Provider
    for entry in entries:
        pid = entry.get("provider_id")
        limit = int(entry.get("daily_token_limit", 0))
        used = int(by_pid.get(pid, {}).get("tokens", 0)) if pid else 0
        provider_name = ""
        if pid:
            p = await db.get(Provider, pid)
            provider_name = p.name if p else ""
        out.append({
            "provider_id": pid,
            "provider_name": provider_name,
            "label": entry.get("label", ""),
            "daily_token_limit": limit,
            "used_tokens_today": used,
            "remaining": max(0, limit - used),
            "is_in_cooling": used >= limit,
        })
    return out
