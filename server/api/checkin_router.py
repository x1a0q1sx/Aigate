"""每日签到 API（/admin/api/checkin/*）。

⚠️ prefix 必须是 /admin/api —— server/core/auth.py 的 _is_admin_api_path 只认
/admin/api/ 与 /admin/oauth/ 两个前缀；用别的前缀未登录时会返回 200+index.html
而不是 401 JSON，前端 JSON.parse 直接炸。

端点：
- GET  /checkin/overview  页面主数据（账号列表 + 今日状态 + 额度 + 能力矩阵 + 运行时间）
- POST /checkin/run       一键签到（可按 provider 过滤）
- GET  /checkin/logs      签到历史
- GET/PUT /checkin/config 配置（自动开关 / 时间 / 启动补签）
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import AsyncSessionLocal
from server.models.checkin_log import CheckinLog
from server.models.oauth_token import OAuthToken

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api")

CST = timezone(timedelta(hours=8))


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


def _cst_day_start_utc() -> datetime:
    """北京时间今天的零点，换算成库内使用的 naive UTC。

    签到「今日」按北京时间划分——上游活动按 UTC+8 刷新（Qoder 每日 10:00）。
    """
    now_cst = datetime.now(CST)
    start_cst = now_cst.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_cst.astimezone(timezone.utc).replace(tzinfo=None)


def _next_run_at(cfg) -> Optional[str]:
    """下次自动签到时间（北京时间 ISO）。"""
    if not cfg or not getattr(cfg, "enabled", False):
        return None
    now = datetime.now(CST)
    target = now.replace(hour=int(getattr(cfg, "hour", 10) or 0),
                         minute=int(getattr(cfg, "minute", 30) or 0),
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.isoformat()


@router.get("/checkin/overview")
async def checkin_overview(days: int = Query(7, ge=1, le=90),
                           with_usage: bool = Query(True),
                           db: AsyncSession = Depends(get_db)):
    """签到页主数据。

    返回按 provider 分组的账号列表，每账号含：
    - 今日状态（从 checkin_logs 派生，不双写）
    - 额度（复用 get_connection_usage，5 分钟缓存；with_usage=false 可跳过）
    - 能力矩阵（哪些平台支持签到）
    """
    from server.config import get_config
    from server.core.checkin import CHECKIN_CAPABILITIES
    from server.core.oauth_client import get_oauth_client

    cfg = getattr(get_config(), "checkin", None)
    client = get_oauth_client()
    day_start = _cst_day_start_utc()

    # 今日所有签到记录（一次查完，按 (code, owner) 归集）
    today_rows = (await db.execute(
        select(CheckinLog).where(CheckinLog.created_at >= day_start)
        .order_by(CheckinLog.id)
    )).scalars().all()
    today_map = {}
    for r in today_rows:
        today_map[(r.provider_code, r.owner)] = r

    # 近 N 天统计（每天每 provider 的成功数/积分）
    hist_rows = (await db.execute(
        select(CheckinLog).where(
            CheckinLog.created_at >= day_start - timedelta(days=days - 1)
        ).order_by(desc(CheckinLog.created_at))
    )).scalars().all()

    # 所有 OAuth 连接（签到页要显示全部账号，含不支持的）
    conn_rows = (await db.execute(
        select(OAuthToken).where(OAuthToken.is_active.is_(True))
        .order_by(OAuthToken.provider_code, OAuthToken.id)
    )).scalars().all()

    providers = []
    seen_codes = []
    for code in CHECKIN_CAPABILITIES:
        accounts = []
        for c in conn_rows:
            if c.provider_code != code:
                continue
            rec = today_map.get((code, c.owner))
            item = {
                "id": c.id, "owner": c.owner,
                "today_kind": rec.kind if rec else None,
                "today_credit": rec.credit if rec else None,
                "today_streak_days": rec.streak_days if rec else None,
                "today_total_credits": rec.total_credits if rec else None,
                "today_activity": (rec.activity_name or None) if rec else None,
                "today_message": (rec.message or None) if rec else None,
                "today_error": (rec.error or None) if rec else None,
                "today_at": rec.created_at.isoformat() + "Z" if rec and rec.created_at else None,
                "today_trigger": rec.trigger if rec else None,
            }
            if with_usage:
                try:
                    token = client._crypto.decrypt(c.access_token_enc)
                    from server.core.oauth_usage import get_connection_usage
                    usage = await get_connection_usage(code, token)
                    item["usage"] = usage
                except Exception as e:
                    item["usage"] = {"quotas": {}, "message": f"额度查询失败：{type(e).__name__}"}
            accounts.append(item)
        if not accounts:
            continue
        seen_codes.append(code)
        done = sum(1 for a in accounts if a["today_kind"] in ("claimed", "already_claimed"))
        earned = sum(a["today_credit"] or 0 for a in accounts if a["today_kind"] == "claimed")
        providers.append({
            "provider_code": code,
            "supported": CHECKIN_CAPABILITIES[code],
            "accounts": accounts,
            "done_today": done,
            "total_accounts": len(accounts),
            "credit_today": round(earned, 2),
        })

    # 汇总
    total_accounts = sum(p["total_accounts"] for p in providers)
    total_done = sum(p["done_today"] for p in providers)
    total_credit = round(sum(p["credit_today"] for p in providers), 2)
    last_run = today_rows[-1] if today_rows else None

    return {
        "providers": providers,
        "summary": {
            "total_accounts": total_accounts,
            "done_today": total_done,
            "pending_today": max(0, total_accounts - total_done),
            "credit_today": total_credit,
            "last_run_at": last_run.created_at.isoformat() + "Z" if last_run and last_run.created_at else None,
            "last_run_trigger": last_run.trigger if last_run else None,
            "next_run_at": _next_run_at(cfg),
        },
        "config": cfg.model_dump() if cfg else {},
        "capabilities": CHECKIN_CAPABILITIES,
        "history": [{
            "id": r.id, "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            "provider_code": r.provider_code, "owner": r.owner, "trigger": r.trigger,
            "kind": r.kind, "credit": r.credit, "streak_days": r.streak_days,
            "activity_name": r.activity_name, "message": r.message, "error": r.error,
        } for r in hist_rows[:200]],
    }


class CheckinRunPayload(BaseModel):
    provider_code: Optional[str] = None   # 省略 = 全部支持的平台


@router.post("/checkin/run")
async def checkin_run(payload: CheckinRunPayload = None,
                      db: AsyncSession = Depends(get_db)):
    """一键签到（严格串行执行，见 core/checkin.py）。

    已完成的账号自动跳过（幂等）。返回逐账号结果。
    """
    from server.core.checkin import collect_targets, run_checkin_batch, CHECKIN_CAPABILITIES

    code = (payload.provider_code if payload else None) or None
    if code and code not in CHECKIN_CAPABILITIES:
        raise HTTPException(status_code=400, detail=f"未知平台：{code}")

    targets, skipped = await collect_targets(db)
    if code:
        targets = [t for t in targets if t.provider_code == code]
        skipped = [s for s in skipped if s["provider_code"] == code]

    if not targets:
        return {"ok": True, "ran": 0, "results": [], "skipped": skipped,
                "message": "没有需要签到的账号（已全部完成或不支持）"}

    results = await run_checkin_batch(targets, trigger="manual", db=db)
    ok = sum(1 for r in results if r["kind"] in ("claimed", "already_claimed"))
    return {"ok": True, "ran": len(results), "succeeded": ok,
            "results": results, "skipped": skipped}


@router.get("/checkin/logs")
async def checkin_logs(days: int = Query(7, ge=1, le=90),
                       provider_code: Optional[str] = Query(None),
                       limit: int = Query(200, ge=1, le=1000),
                       db: AsyncSession = Depends(get_db)):
    """签到历史（近 N 天，可按平台过滤）。"""
    since = _cst_day_start_utc() - timedelta(days=days - 1)
    q = select(CheckinLog).where(CheckinLog.created_at >= since)
    if provider_code:
        q = q.where(CheckinLog.provider_code == provider_code)
    rows = (await db.execute(q.order_by(desc(CheckinLog.created_at)).limit(limit))).scalars().all()
    return [{
        "id": r.id,
        "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
        "provider_code": r.provider_code, "owner": r.owner, "trigger": r.trigger,
        "kind": r.kind, "credit": r.credit, "streak_days": r.streak_days,
        "total_credits": r.total_credits, "activity_name": r.activity_name,
        "message": r.message, "error": r.error, "upstream_code": r.upstream_code,
        "duration_ms": r.duration_ms,
    } for r in rows]


class CheckinConfigModel(BaseModel):
    enabled: Optional[bool] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    startup_catchup: Optional[bool] = None


@router.get("/checkin/config")
async def get_checkin_config():
    from server.config import get_config
    cfg = getattr(get_config(), "checkin", None)
    return cfg.model_dump() if cfg else {}


@router.put("/checkin/config")
async def put_checkin_config(body: CheckinConfigModel):
    from server.config import get_config, save_config
    cfg = getattr(get_config(), "checkin", None)
    if cfg is None:
        raise HTTPException(status_code=500, detail="checkin 配置段缺失")
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is None:
            continue
        # 钳制到合法区间，防止配错把定时器打瘫
        if k == "hour":
            v = max(0, min(23, int(v)))
        elif k == "minute":
            v = max(0, min(59, int(v)))
        setattr(cfg, k, v)
    save_config()
    # 配置变更后重排定时任务（开关/时间点即时生效，无需重启）
    try:
        from server.main import reschedule_checkin_job
        reschedule_checkin_job()
    except Exception as e:
        logger.warning("checkin reschedule failed: %s", e)
    return cfg.model_dump()
