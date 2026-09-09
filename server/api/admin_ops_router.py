"""管理运维 API（v2 路线第三批）：

- GET  /admin/api/live/summary        B1 实时监控摘要（前端 3s 轮询）
- GET  /admin/api/analytics/failures  B2 失败分析看板（按错误类型/服务商分组）
- POST /admin/api/diagnose            B4 一键诊断（模型或组合真实探测）
- GET  /admin/api/logs/export         B5 日志导出 CSV/JSON（按筛选条件）
- GET/PUT /admin/api/notify           D3 通知渠道配置
- POST /admin/api/notify/test         D3 发送测试通知
- GET  /admin/api/price-health        D4 价格健康度（缺价/零价模型清单）
"""
import csv
import io
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func, desc, case
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import AsyncSessionLocal
from server.models.model import Model
from server.models.provider import Provider
from server.models.request_log import RequestLog
from server.models.combo import Combo

router = APIRouter(prefix="/admin/api")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ─────────────────────────── B4: 一键诊断 ───────────────────────────

class DiagnoseRequest(BaseModel):
    model_id: Optional[int] = None      # models.id
    combo_id: Optional[int] = None      # combos.id（对组合逐个候选诊断）
    max_targets: int = 6


async def _diagnose_one(db: AsyncSession, model: Model, provider: Provider) -> dict:
    """真实小请求探测：复用 HealthChecker.check_model（覆盖 free/oauth/标准密钥/代理）。"""
    from server.main import get_health_checker
    from server.core.key_manager import KeyManager
    hc = get_health_checker()
    t0 = time.time()
    hr = await hc.check_model(db, model, provider, KeyManager())
    info = {
        "model_id": model.id,
        "full_id": f"{provider.name}/{model.model_id}",
        "ok": hr.status in ("healthy", "degraded"),
        "status": hr.status,
        "latency_ms": int(hr.latency_ms or 0),
        "error": hr.error_message or "",
        "elapsed_ms": int((time.time() - t0) * 1000),
        "context_length": model.context_length,
        "input_price": model.input_price,
        "output_price": model.output_price,
        "is_free": bool(model.is_free),
        "auto_enabled": bool(model.auto_enabled),
        "enabled": bool(model.enabled),
    }
    if info["ok"] and info["latency_ms"]:
        info["tps_est"] = None  # 探测请求太短，不估算 TPS，避免误导
    return info


@router.post("/diagnose")
async def diagnose(body: DiagnoseRequest, db: AsyncSession = Depends(get_db)):
    """模型或组合的真实连通性诊断报告"""
    if body.combo_id:
        combo = await db.get(Combo, body.combo_id)
        if not combo:
            raise HTTPException(status_code=404, detail="Combo not found")
        from server.core.combo_router import resolve_combo_targets
        targets = await resolve_combo_targets(db, combo)
        results = []
        for t in targets[: max(1, min(body.max_targets, 12))]:
            results.append(await _diagnose_one(db, t["model"], t["provider"]))
        ok_n = sum(1 for r in results if r["ok"])
        return {"combo": combo.name, "total": len(results), "ok": ok_n, "results": results}
    if body.model_id:
        model = await db.get(Model, body.model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
        provider = await db.get(Provider, model.provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        r = await _diagnose_one(db, model, provider)
        return {"total": 1, "ok": 1 if r["ok"] else 0, "results": [r]}
    raise HTTPException(status_code=400, detail="model_id 或 combo_id 必填其一")


# ─────────────────────────── B2: 失败分析 ───────────────────────────

@router.get("/analytics/failures")
async def failure_analysis(
    hours: int = Query(24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
):
    """近 N 小时失败请求分组：按 error_type / 按服务商 / 按模型"""
    since = datetime.utcnow() - timedelta(hours=hours)
    base = (RequestLog.created_at >= since, RequestLog.is_health_check.is_(False),
            RequestLog.status == "error")

    by_type = (await db.execute(
        select(RequestLog.error_type, func.count(RequestLog.id),
               func.max(RequestLog.created_at))
        .where(*base).group_by(RequestLog.error_type)
        .order_by(desc(func.count(RequestLog.id)))
    )).all()
    by_provider = (await db.execute(
        select(RequestLog.routed_provider, func.count(RequestLog.id),
               func.max(RequestLog.created_at))
        .where(*base).group_by(RequestLog.routed_provider)
        .order_by(desc(func.count(RequestLog.id)))
    )).all()
    by_model = (await db.execute(
        select(RequestLog.routed_provider, RequestLog.routed_model,
               func.count(RequestLog.id))
        .where(*base).group_by(RequestLog.routed_provider, RequestLog.routed_model)
        .order_by(desc(func.count(RequestLog.id))).limit(50)
    )).all()
    sample = (await db.execute(
        select(RequestLog.error_msg).where(*base, RequestLog.error_msg.is_not(None))
        .order_by(desc(RequestLog.created_at)).limit(1)
    )).scalar()

    def _iso(dt):
        return dt.isoformat(timespec="seconds") if dt else None

    return {
        "hours": hours,
        "total_errors": sum(c for _, c, *_ in by_type),
        "by_error_type": [{"error_type": t or "unknown", "count": c, "last_seen": _iso(l)} for t, c, l in by_type],
        "by_provider": [{"provider": p or "-", "count": c, "last_seen": _iso(l)} for p, c, l in by_provider],
        "by_model": [{"provider": p or "-", "model": m or "-", "count": c} for p, m, c in by_model],
        "latest_error_sample": (sample or "")[:500],
    }


# ─────────────────────────── B1: 实时监控摘要 ───────────────────────────

@router.get("/live/summary")
async def live_summary(db: AsyncSession = Depends(get_db)):
    """监控页轮询数据：今日概况 + 冷却中模型 + 最近请求 + 组件状态"""
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    row = (await db.execute(
        select(func.count(RequestLog.id),
               func.coalesce(func.sum(case((RequestLog.status == "success", 1), else_=0)), 0),
               func.coalesce(func.sum(case((RequestLog.status == "pending", 1), else_=0)), 0),
               func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
               func.coalesce(func.sum(RequestLog.completion_tokens), 0),
               func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0))
        .where(RequestLog.created_at >= day_start, RequestLog.is_health_check.is_(False))
    )).first()
    total, success, pending, pt, ct, cost = row

    # 冷却中模型
    cooling = []
    try:
        from server.main import get_health_checker
        hc = get_health_checker()
        now = datetime.utcnow()
        for mid, until in list(getattr(hc, "_cooling", {}).items()):
            if until and until > now:
                m = await db.get(Model, mid)
                cooling.append({
                    "model_id": mid,
                    "model_id_str": m.model_id if m else f"#{mid}",
                    "provider": (await db.get(Provider, m.provider_id)).name if m and m.provider_id else "",
                    "until": until.isoformat(timespec="seconds"),
                })
    except Exception:
        pass

    recent = (await db.execute(
        select(RequestLog.id, RequestLog.requested_model, RequestLog.routed_provider,
               RequestLog.routed_model, RequestLog.status, RequestLog.latency_ms,
               RequestLog.ttft_ms, RequestLog.error_msg, RequestLog.created_at)
        .where(RequestLog.is_health_check.is_(False))
        .order_by(desc(RequestLog.id)).limit(15)
    )).all()

    from server.core.log_queue import stats as lq_stats
    try:
        from server.core.response_cache import response_cache
        cache_info = response_cache.info()
    except Exception:
        cache_info = {}

    def _iso(dt):
        return dt.isoformat(timespec="seconds") if dt else None

    return {
        "today": {
            "requests": int(total or 0),
            "success": int(success or 0),
            "pending": int(pending or 0),
            "errors": int(total or 0) - int(success or 0) - int(pending or 0),
            "prompt_tokens": int(pt or 0),
            "completion_tokens": int(ct or 0),
            "cost_usd": round(float(cost or 0), 4),
        },
        "cooling": sorted(cooling, key=lambda x: x["until"]),
        "recent": [{
            "id": r.id, "requested_model": r.requested_model,
            "routed_provider": r.routed_provider, "routed_model": r.routed_model,
            "status": r.status, "latency_ms": r.latency_ms, "ttft_ms": r.ttft_ms,
            "error": (r.error_msg or "")[:120], "created_at": _iso(r.created_at),
        } for r in recent],
        "log_queue": dict(lq_stats),
        "cache": cache_info,
        "server_time": datetime.utcnow().isoformat(timespec="seconds"),
    }


# ─────────────────────────── B5: 日志导出 ───────────────────────────

@router.get("/logs/export")
async def export_logs(
    format: str = Query("csv", pattern="^(csv|json)$"),
    hours: int = Query(168, ge=1, le=24 * 90),
    status: Optional[str] = None,
    provider: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """按当前筛选导出请求日志（近 N 小时，上限 50000 行）"""
    since = datetime.utcnow() - timedelta(hours=hours)
    conds = [RequestLog.created_at >= since, RequestLog.is_health_check.is_(False)]
    if status:
        conds.append(RequestLog.status == status)
    if provider:
        conds.append(RequestLog.routed_provider == provider)
    rows = (await db.execute(
        select(RequestLog).where(*conds).order_by(desc(RequestLog.id)).limit(50000)
    )).scalars().all()
    fields = ["id", "created_at", "requested_model", "routed_provider", "routed_model",
              "status", "media_type", "http_status", "latency_ms", "ttft_ms",
              "prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens",
              "estimated_cost_usd", "error_type", "error_msg", "fallback_count",
              "user_ip", "used_proxy", "downstream_key_id"]
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    if format == "json":
        data = []
        for r in rows:
            data.append({f: (getattr(r, f).isoformat(timespec="seconds")
                             if f == "created_at" and getattr(r, f) else getattr(r, f))
                         for f in fields})
        return StreamingResponse(
            iter([json.dumps(data, ensure_ascii=False, default=str)]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=aigate-logs-{stamp}.json"})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for r in rows:
        w.writerow([r.created_at.isoformat(timespec="seconds") if r.created_at else "",
                    *[_getattr_safe(r, f) for f in fields[1:]]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=aigate-logs-{stamp}.csv"})


def _getattr_safe(r, f):
    v = getattr(r, f, None)
    return "" if v is None else v


# ─────────────────────────── D3: 通知配置 ───────────────────────────

class NotifyConfigModel(BaseModel):
    enabled: Optional[bool] = None
    webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    dingtalk_webhook: Optional[str] = None
    notify_model_cooldown: Optional[bool] = None
    notify_all_failed: Optional[bool] = None
    notify_budget_exceeded: Optional[bool] = None
    min_interval_seconds: Optional[int] = None


@router.get("/notify")
async def get_notify_config():
    from server.config import get_config
    return get_config().notify.model_dump()


@router.put("/notify")
async def put_notify_config(body: NotifyConfigModel):
    from server.config import get_config, save_config
    cfg = get_config().notify
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(cfg, k, v)
    save_config()
    return cfg.model_dump()


@router.post("/notify/test")
async def notify_test():
    from server.core.notifier import send_test
    results = await send_test()
    if not results:
        return {"ok": False, "detail": "未配置任何渠道或 notify.enabled=false", "results": results}
    ok = all(not str(v).startswith("err") for v in results.values())
    return {"ok": ok, "results": results}


# ─────────────────────────── D4: 价格健康度 ───────────────────────────

@router.get("/price-health")
async def price_health(db: AsyncSession = Depends(get_db)):
    """缺价/零价模型清单（is_free 除外）：模型管理页标黄提醒"""
    rows = (await db.execute(
        select(Model, Provider)
        .join(Provider, Provider.id == Model.provider_id)
        .where(Model.enabled.is_(True), Model.is_free.is_(False))
    )).all()
    missing, zero = [], []
    for m, p in rows:
        item = {"model_id": m.id, "full_id": f"{p.name}/{m.model_id}",
                "input_price": m.input_price, "output_price": m.output_price,
                "pricing_source": getattr(m, "pricing_source", "") or "",
                "auto_enabled": bool(m.auto_enabled)}
        if (m.input_price or 0) == 0 and (m.output_price or 0) == 0:
            (missing if not (getattr(m, "pricing_source", "") or "") else zero).append(item)
    return {
        "missing_price": missing[:200],      # 无 pricing_source → 从未抓到价
        "zero_price": zero[:200],            # 站点报了 0 价（可能按倍率计费）
        "missing_count": len(missing),
        "zero_count": len(zero),
    }


# ─────────────────────────── B3: 模型批量操作 ───────────────────────────

class BatchModelRequest(BaseModel):
    ids: list
    action: str   # enable / disable / auto_include / auto_exclude / delete


@router.post("/models/batch")
async def batch_models(body: BatchModelRequest, db: AsyncSession = Depends(get_db)):
    """模型管理页批量操作：启用/禁用/加入或移出 Auto/删除"""
    ids = [int(i) for i in (body.ids or [])][:2000]
    if not ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    field_map = {
        "enable": {"enabled": True},
        "disable": {"enabled": False},
        "auto_include": {"auto_enabled": True, "auto_excluded": False},
        "auto_exclude": {"auto_enabled": False, "auto_excluded": True},
    }
    if body.action == "delete":
        from sqlalchemy import delete as sqldelete
        from server.models.model_api_key import ModelApiKey
        await db.execute(sqldelete(ModelApiKey).where(ModelApiKey.model_id.in_(ids)))
        result = await db.execute(sqldelete(Model).where(Model.id.in_(ids)))
        await db.commit()
        return {"action": "delete", "affected": result.rowcount}
    if body.action not in field_map:
        raise HTTPException(status_code=400, detail=f"不支持的动作: {body.action}")
    updates = field_map[body.action]
    result = await db.execute(
        Model.__table__.update().where(Model.id.in_(ids)).values(**updates)
    )
    await db.commit()
    return {"action": body.action, "affected": result.rowcount}
