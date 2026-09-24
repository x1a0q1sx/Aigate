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


# ─────────────────────────── DroolGuard: 流口水自动冷却 ───────────────────────────

class DroolGuardModel(BaseModel):
    enabled: Optional[bool] = None
    threshold: Optional[int] = None
    cooldown_minutes: Optional[int] = None
    min_answer_chars: Optional[int] = None


@router.get("/drool-guard")
async def get_drool_guard():
    from server.config import get_config
    return get_config().drool_guard.model_dump()


@router.put("/drool-guard")
async def put_drool_guard(body: DroolGuardModel):
    """保存设置并清空内存中的连续计数（阈值改了从头算，避免旧链用新阈值误触发）。"""
    from server.config import get_config, save_config
    cfg = get_config().drool_guard
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(cfg, k, v)
    save_config()
    try:
        from server.core.drool_guard import reset_state
        reset_state()
    except Exception:
        pass
    return cfg.model_dump()


# ─────────────────────────── Race: 候选竞速 ───────────────────────────

class RaceModel(BaseModel):
    enabled: Optional[bool] = None
    no_content_seconds: Optional[int] = None


@router.get("/race")
async def get_race_config():
    from server.config import get_config
    return get_config().race.model_dump()


@router.put("/race")
async def put_race_config(body: RaceModel):
    from server.config import get_config, save_config
    cfg = get_config().race
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(cfg, k, v)
    save_config()
    return cfg.model_dump()


# ─────────────────────────── 模型刷新：超时与并发 ───────────────────────────
# 用户反馈「模型刷新太慢」：此前 57 个服务商串行刷新，平均 17s/个、最慢 141s。
# 现可配置并发数与单服务商硬超时（超时判失败不等待）。

class ModelRefreshConfigModel(BaseModel):
    timeout_seconds: Optional[int] = None            # 单次网络请求超时（list_models / pricing 各算一次）
    provider_timeout_seconds: Optional[int] = None   # 单个服务商整体硬超时（超时判失败）
    concurrency: Optional[int] = None                # 全量刷新并发数（1=串行）
    scheduled_enabled: Optional[bool] = None
    interval_minutes: Optional[int] = None
    remove_missing_models: Optional[bool] = None


@router.get("/model-refresh")
async def get_model_refresh_config():
    from server.config import get_config
    return get_config().model_refresh.model_dump()


@router.put("/model-refresh")
async def put_model_refresh_config(body: ModelRefreshConfigModel):
    from server.config import get_config, save_config
    cfg = get_config().model_refresh
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is None:
            continue
        # 钳制到合理区间，防止配 0/负数把刷新打瘫
        if k == "timeout_seconds":
            v = max(3, min(600, int(v)))
        elif k == "provider_timeout_seconds":
            v = max(5, min(1800, int(v)))
        elif k == "concurrency":
            v = max(1, min(32, int(v)))
        elif k == "interval_minutes":
            v = max(5, min(43200, int(v)))
        setattr(cfg, k, v)
    save_config()
    return cfg.model_dump()


# ─────────────────────────── OpenCode 桥接（官方 CLI sidecar） ───────────────────────────

class OpenCodeBridgeModel(BaseModel):
    enabled: Optional[bool] = None
    timeout_seconds: Optional[int] = None
    poll_interval_ms: Optional[int] = None
    stall_grace_seconds: Optional[int] = None
    agent: Optional[str] = None
    base_url: Optional[str] = None
    port: Optional[int] = None
    bin_path: Optional[str] = None
    auto_start: Optional[bool] = None
    manage_agent_config: Optional[bool] = None
    auto_reject_tools: Optional[bool] = None


@router.get("/opencode")
async def get_opencode_bridge():
    """OpenCode 桥接配置 + 实时状态（sidecar 是否在线、CLI agent 是否就绪）。"""
    from server.config import get_config
    from server.core import opencode_sidecar as sc
    cfg = get_config()
    bridge = getattr(cfg, "opencode_bridge", None)
    data = bridge.model_dump() if bridge is not None else {}
    alive = False
    try:
        alive = await sc.sidecar_alive()
    except Exception:
        alive = False
    agent_ok, agent_note = False, "未检查"
    if getattr(bridge, "manage_agent_config", True):
        try:
            agent_ok, agent_note = sc.ensure_bridge_agent_config()
        except Exception as e:
            agent_note = f"检查失败: {e}"
    return {
        "config": data,
        "status": {
            "sidecar_alive": alive,
            "agent_ok": agent_ok,
            "agent_note": agent_note,
            "effective": {
                "base_url": sc.sidecar_base(),
                "timeout_seconds": sc.sidecar_timeout(),
                "poll_interval_ms": int(sc.sidecar_poll_interval() * 1000),
                "stall_grace_seconds": int(sc.stall_grace_seconds()),
                "agent": sc.sidecar_agent(),
                "auto_reject_tools": sc.auto_reject_tools(),
            },
        },
    }


@router.put("/opencode")
async def put_opencode_bridge(body: OpenCodeBridgeModel):
    """保存桥接配置（立即生效：超时/轮询/agent/自动拒绝工具都按请求时读取）。

    数值做边界收敛，避免把网关自己配成永远等不到结果：
    timeout 10–1800s、轮询 50–5000ms、端口 1–65535。
    """
    from server.config import get_config, save_config
    cfg = get_config()
    if getattr(cfg, "opencode_bridge", None) is None:
        from server.config import OpenCodeBridgeConfig
        cfg.opencode_bridge = OpenCodeBridgeConfig()
    bridge = cfg.opencode_bridge
    patch = body.model_dump(exclude_unset=True)
    if "timeout_seconds" in patch:
        patch["timeout_seconds"] = max(10, min(1800, int(patch["timeout_seconds"])))
    if "poll_interval_ms" in patch:
        patch["poll_interval_ms"] = max(50, min(5000, int(patch["poll_interval_ms"])))
    if "stall_grace_seconds" in patch:
        patch["stall_grace_seconds"] = max(5, min(300, int(patch["stall_grace_seconds"])))
    if "port" in patch:
        patch["port"] = max(1, min(65535, int(patch["port"])))
    if "agent" in patch:
        patch["agent"] = (patch["agent"] or "").strip()
    if "base_url" in patch:
        patch["base_url"] = (patch["base_url"] or "").strip()
    for k, v in patch.items():
        setattr(bridge, k, v)
    save_config()
    # agent 名/权限开关变了 → 立刻把 CLI 侧定义同步过去（否则要等下一轮守护）
    note = ""
    try:
        from server.core import opencode_sidecar as sc
        if bridge.manage_agent_config:
            _, note = sc.ensure_bridge_agent_config()
    except Exception as e:
        note = f"同步 CLI agent 失败: {e}"
    return {"config": bridge.model_dump(), "agent_note": note}


@router.post("/opencode/restart")
async def restart_opencode_bridge():
    """重启 sidecar（改端口/换 CLI 路径后无需登服务器）。"""
    import asyncio as _aio
    from server.config import get_config
    from server.core import opencode_sidecar as sc
    cfg = get_config()
    bridge = getattr(cfg, "opencode_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=400, detail="配置缺失")
    if not bridge.auto_start:
        raise HTTPException(status_code=400, detail="auto_start=false，网关不会拉起 sidecar")
    # 先停掉旧进程（按端口 + 名字匹配，避免误杀别的进程）
    stop = await _aio.create_subprocess_exec(
        "pkill", "-f", f"opencode serve --port {int(bridge.port)}",
        stdout=_aio.subprocess.DEVNULL, stderr=_aio.subprocess.DEVNULL)
    await stop.wait()
    await _aio.sleep(1)
    import shutil as _shutil
    import os as _os
    bin_path = (bridge.bin_path or "").strip() or _os.environ.get("AIGATE_OPENCODE_BIN") or _os.path.join(
        _os.path.expanduser("~"), "opencode", "bin", "opencode")
    exe = bin_path if _os.path.exists(bin_path) else _shutil.which("opencode")
    if not exe:
        raise HTTPException(status_code=400, detail="未找到 opencode 可执行文件（可设置 bin_path）")
    if bridge.manage_agent_config:
        sc.ensure_bridge_agent_config()
    await _aio.create_subprocess_exec(
        exe, "serve", "--port", str(int(bridge.port)), "--hostname", "127.0.0.1",
        stdout=_aio.subprocess.DEVNULL, stderr=_aio.subprocess.DEVNULL,
        start_new_session=True)
    await _aio.sleep(8)
    alive = await sc.sidecar_alive()
    return {"ok": alive, "sidecar_alive": alive,
            "detail": "sidecar 已重启" if alive else "启动后仍不可用，请检查 CLI 是否可执行"}


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
