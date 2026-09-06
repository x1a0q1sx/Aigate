"""
Admin API - v0.2: 排行榜 / 请求日志 / 人工干预 / 审计 / 日志归档
"""
import os
import gzip
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, desc, func, and_
from sqlalchemy.orm import defer
from server.db import AsyncSessionLocal
from server.models.request_log import RequestLog, AnalyticsCumulative
from server.core.request_logger import reassemble_request, reassemble_response  # v3.6 消息级去重还原
from server.models.intelligence import IntelligenceStatic
from server.models.routing_config import RoutingWeights, RoutingPin, AdminAuditLog
from server.models.model import Model
from server.models.provider import Provider
from server.models.api_key import ApiKey
from server.core.ranking_service import RankingService
from server.core.health_checker import HealthChecker
from server.core.key_rotator import get_key_rotator
from server.core.proxy_pool import get_proxy_pool
from server.config import get_config
router = APIRouter(prefix="/admin/api", tags=["admin-v0.2"])
_rs = RankingService()
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
async def _audit(db: AsyncSession, action: str, target_id: Optional[int] = None,
                 payload: Optional[dict] = None, actor: str = "admin"):
    rec = AdminAuditLog(
        actor=actor, action=action, target_id=target_id,
        payload=payload or {},
    )
    db.add(rec)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
# ===================== 请求诊断日志开关 =====================
@router.get("/diag")
async def get_diag():
    """读取请求诊断日志是否全量输出（verbose_diag）+ 日志写入队列统计（P0-3）"""
    from server.api.v1_router import get_diag_verbose
    try:
        from server.core.log_queue import stats as _lq_stats
        lq = dict(_lq_stats)
    except Exception:
        lq = None
    return {"verbose": get_diag_verbose(), "log_queue": lq}

@router.put("/diag")
async def set_diag(verbose: bool = Query(..., description="true=全量输出所有阶段；false=仅关键里程碑"),
                  db: AsyncSession = Depends(get_db)):
    """开关请求诊断日志（持久化到 config.yaml，重启后仍生效）"""
    from server.api.v1_router import set_diag_verbose
    set_diag_verbose(verbose)
    await _audit(db, "diag.set", payload={"verbose": verbose})
    return {"ok": True, "verbose": verbose}
# ===================== 失败罚时 / 冷却总览 =====================
@router.get("/cooling")
async def get_cooling(db: AsyncSession = Depends(get_db)):
    """
    聚合三处「失败罚时 / 冷却」状态，供前端运维面板实时展示：
      1) 模型冷却（HealthChecker 单例，真实流量驱动：失败→指数退避 30s~1h）
      2) 密钥冷却（KeyRotator 单例：连续 3 次失败→熔断冷却 60s，401/403 永久禁用）
      3) 代理冷却（ProxyPool 单例：连续 3 次失败→冷却 30s）
    每条带 cooldown_until（ISO，UTC 带 Z）与 remaining_sec（服务端实时计算），
    前端可据此做倒计时；remaining_sec<=0 表示已恢复。
    """
    from server.main import get_health_checker
    now = datetime.utcnow()

    # 1) 模型冷却（真实流量驱动；内存 + DB 持久化状态合并）
    model_cooling = []
    hc = get_health_checker()
    mem_ids = (set(hc._cooling.keys()) | set(hc._fail_count.keys())) if hc is not None else set()
    db_rows = (await db.execute(
        select(Model, Provider).join(Provider, Model.provider_id == Provider.id)
        .where((Model.auto_cooldown_until != None) | (Model.auto_fail_count > 0) | (Model.id.in_(mem_ids) if mem_ids else False))
    )).all()
    seen_ids = set()
    for mdl, prov in db_rows:
        mid = mdl.id
        seen_ids.add(mid)
        mem_cd = hc._cooling.get(mid) if hc is not None else None
        mem_fc = hc._fail_count.get(mid, 0) if hc is not None else 0
        db_cd = getattr(mdl, "auto_cooldown_until", None)
        db_fc = int(getattr(mdl, "auto_fail_count", 0) or 0)
        cd = mem_cd or db_cd
        fc = max(mem_fc, db_fc)
        if cd and now >= cd:
            cd = None
        if cd is None and fc == 0:
            continue
        remaining = max(0, int((cd - now).total_seconds())) if cd else 0
        model_cooling.append({
            "model_id": mid,
            "model_full_id": f"{prov.name}/{mdl.model_id}",
            "provider": prov.name,
            "fail_count": fc,
            "cooldown_until": cd.isoformat() + "Z" if cd else None,
            "remaining_sec": remaining,
            "cooling": bool(cd and now < cd),
        })
    # 理论兜底：内存里有但 DB 查不到的模型
    for mid in mem_ids - seen_ids:
        cd = hc._cooling.get(mid) if hc is not None else None
        fc = hc._fail_count.get(mid, 0) if hc is not None else 0
        if cd is None and fc == 0:
            continue
        remaining = max(0, int((cd - now).total_seconds())) if cd else 0
        model_cooling.append({
            "model_id": mid,
            "model_full_id": f"model#{mid}",
            "provider": None,
            "fail_count": fc,
            "cooldown_until": cd.isoformat() + "Z" if cd else None,
            "remaining_sec": remaining,
            "cooling": bool(cd and now < cd),
        })
    model_cooling.sort(key=lambda x: x["remaining_sec"], reverse=True)

    # 2) 密钥冷却
    key_cooling = []
    rot = get_key_rotator()
    krows = (await db.execute(
        select(ApiKey, Provider).join(Provider, ApiKey.provider_id == Provider.id)
    )).all()
    kmap = {k.id: p.name for k, p in krows}
    snap = rot.status_snapshot()
    for kid, cd_iso in snap["cooldown_until"].items():
        cd = datetime.fromisoformat(cd_iso.replace("Z", ""))
        key_cooling.append({
            "api_key_id": kid,
            "provider": kmap.get(kid, "未知"),
            "fail_count": snap["fail_count"].get(kid, 0),
            "cooldown_until": cd_iso,
            "remaining_sec": max(0, int((cd - now).total_seconds())),
            "hard_disabled": kid in snap["hard_disabled"],
        })
    for kid in snap["hard_disabled"]:
        if not any(k["api_key_id"] == kid for k in key_cooling):
            key_cooling.append({
                "api_key_id": kid,
                "provider": kmap.get(kid, "未知"),
                "fail_count": snap["fail_count"].get(kid, 0),
                "cooldown_until": None,
                "remaining_sec": 0,
                "hard_disabled": True,
            })
    key_cooling.sort(key=lambda x: x["remaining_sec"], reverse=True)

    # 3) 代理冷却
    proxy_cooling = []
    p_snap = get_proxy_pool().status_snapshot()
    for p in p_snap.get("proxies", []):
        cd_iso = p.get("cooldown_until")
        remaining = 0
        if cd_iso:
            cd = datetime.fromisoformat(cd_iso.replace("Z", ""))
            remaining = max(0, int((cd - now).total_seconds()))
        if cd_iso or p.get("fail_count", 0) > 0:
            proxy_cooling.append({
                "name": p.get("name"),
                "url": p.get("url"),
                "fail_count": p.get("fail_count", 0),
                "cooldown_until": cd_iso,
                "remaining_sec": remaining,
            })
    proxy_cooling.sort(key=lambda x: x["remaining_sec"], reverse=True)

    return {
        "model_cooling": model_cooling,
        "key_cooling": key_cooling,
        "proxy_cooling": proxy_cooling,
        "summary": {
            "model_cooling_count": len(model_cooling),
            "key_cooling_count": len(key_cooling),
            "proxy_cooling_count": len(proxy_cooling),
            "proxy_enabled": bool(p_snap.get("enabled", False)),
        },
    }
@router.post("/cooling/clear")
async def clear_model_cooling(model_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    """一键清除模型失败冷却惩罚。
    model_id 缺省时清除所有模型的冷却状态与失败计数；
    指定 model_id 时只清除该模型。
    同时清除 HealthChecker 内存状态和 DB 持久化字段。"""
    from server.main import get_health_checker
    hc = get_health_checker()
    cleared = 0
    if hc is not None:
        cleared = hc.clear_cooling(model_id)
    # DB 侧兜底：如果 HealthChecker 的 sqlite3 写入失败，用 async session 再做一次
    if model_id is None:
        result = await db.execute(text(
            "UPDATE models SET auto_cooldown_until=NULL, auto_fail_count=0 "
            "WHERE auto_cooldown_until IS NOT NULL OR auto_fail_count > 0"
        ))
        cleared = max(cleared, result.rowcount if result.rowcount is not None else 0)
    else:
        await db.execute(text(
            "UPDATE models SET auto_cooldown_until=NULL, auto_fail_count=0 WHERE id=:mid"
        ), {"mid": int(model_id)})
    await db.commit()
    await _audit(db, "cooling.clear", payload={"model_id": model_id, "cleared": cleared})
    return {"ok": True, "cleared": cleared}
# ===================== 排行榜 =====================
@router.get("/ranking/top-speed")
async def top_speed(limit: int = Query(5, ge=1, le=50), db: AsyncSession = Depends(get_db)):
    """最快模型 Top N (P50 升序)"""
    return await _rs.rank_top_speed(db, limit)
@router.get("/ranking/top-intel")
async def top_intel(limit: int = Query(10, ge=1, le=50), db: AsyncSession = Depends(get_db)):
    """智力榜"""
    return await _rs.rank_top_intel(db, limit)
@router.get("/ranking/top-stab")
async def top_stab(limit: int = Query(10, ge=1, le=50), db: AsyncSession = Depends(get_db)):
    """稳定性榜"""
    return await _rs.rank_top_stab(db, limit)
@router.get("/ranking/overall")
async def overall_ranking(limit: int = Query(500, ge=1, le=1000),
                            db: AsyncSession = Depends(get_db)):
    """综合评分榜（含权重、三维分项）。
    Auto 选举页只关心「参与 auto 的候选」，故只取 auto_enabled 的模型，
    默认上限提到 500，避免免费模型被截在 top-20 之外看不到。"""
    models = (await db.execute(
        select(Model, Provider).join(Provider, Model.provider_id == Provider.id)
        .where(Model.enabled == True, Model.auto_enabled == True)
    )).all()
    mlist = [m for m, _ in models]
    prov_by_pid = {}
    for m, p in models:
        prov_by_pid[m.provider_id] = p
    weights = await _rs.get_weights(db)
    cooling = {}
    from server.main import get_health_checker
    hc = get_health_checker() or HealthChecker()
    for m in mlist:
        if hc.is_cooling(m.id):
            cooling[m.id] = datetime.utcnow()
    scores = await _rs.rank_all(db, mlist, prov_by_pid, cooling)
    out = []
    for s in scores[:limit]:
        cd_until = None
        fc = hc._fail_count.get(s.model_id, 0) if hc else 0
        cd = hc._cooling.get(s.model_id) if hc else None
        if cd and datetime.utcnow() < cd:
            cd_until = cd.isoformat() + "Z"
        out.append({
            "model_id": s.model_id,
            "provider": s.provider_name,
            "model": s.model_id_str,
            "model_full_id": f"{s.provider_name}/{s.model_id_str}",
            "display_name": s.display_name,
            "is_free": s.is_free,
            "speed_score": s.speed_score,
            "intel_score": s.intel_score,
            "intel_source": s.intel_source,
            "stab_score": s.stab_score,
            "avg_ms": s.avg_ms,
            "success_rate": s.success_rate,
            "final_score": s.final_score,
            "excluded_reason": s.excluded_reason,
            "priority_boost": s.priority_boost,
            "cooldown_until": cd_until,
            "fail_count": fc,
            "weights": weights,
        })
    return {"weights": weights, "ranking": out, "total_candidates": len(scores)}
# ===================== 日志归档 — 归档文件列表（必须在 /logs/{log_id} 之前注册，否则 archives 会被当作 log_id） =====================
@router.get("/logs/archives")
async def list_archives():
    """列出所有归档文件"""
    ad = _get_archive_dir()
    files = sorted(ad.glob("arch-*.jsonl.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    archives = []
    for fp in files:
        info = _parse_archive_meta(fp.name)
        if info:
            archives.append(info)
    return {"archives": archives, "last_archive": _last_archive_info}

# 必须在 /logs/{log_id} 之前注册，否则 providers 会被当作 log_id
@router.get("/logs/providers")
async def list_log_providers(db: AsyncSession = Depends(get_db)):
    """请求日志里出现过的服务商名（去重），供日志筛选下拉使用。"""
    rows = (await db.execute(
        select(RequestLog.routed_provider)
        .where(RequestLog.routed_provider.isnot(None))
        .group_by(RequestLog.routed_provider)
        .order_by(RequestLog.routed_provider)
    )).scalars().all()
    return {"providers": [r for r in rows if r]}

@router.get("/logs/{log_id}")
async def get_log(log_id: int, full: bool = Query(False, description="true 返回完整 body；默认超过 160KB 截断（详情弹窗快速打开）"), db: AsyncSession = Depends(get_db)):
    row = await db.get(RequestLog, log_id)
    if not row:
        raise HTTPException(404, "Log not found")
    req_body = await reassemble_request(db, row)
    resp_body = await reassemble_response(db, row)
    req_trunc = resp_trunc = False
    if not full:
        # Codex 等客户端单条请求 body 可达数 MB，全量返回导致详情弹窗加载/渲染极慢。
        # 默认截断到 160KB；前端可传 full=1 或点「查看完整」拿原文。
        _MAX_BODY = 160 * 1024
        if req_body and len(req_body) > _MAX_BODY:
            total = len(req_body)
            req_body = req_body[:_MAX_BODY] + f"\n\n... [已截断，完整大小 {total//1024} KB，加 ?full=1 查看全部]"
            req_trunc = True
        if resp_body and len(resp_body) > _MAX_BODY:
            total = len(resp_body)
            resp_body = resp_body[:_MAX_BODY] + f"\n\n... [已截断，完整大小 {total//1024} KB，加 ?full=1 查看全部]"
            resp_trunc = True
    return {
        "id": row.id, "conversation_id": row.conversation_id,
        "requested_model": row.requested_model,
        "routed_provider": row.routed_provider, "routed_model": row.routed_model,
        "status": row.status, "media_type": getattr(row, "media_type", None), "http_status": row.http_status,
        "latency_ms": row.latency_ms,
        "ttft_ms": getattr(row, "ttft_ms", None),
        "prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens,
        "estimated_cost_usd": getattr(row, "estimated_cost_usd", None),
        "cache_read_tokens": getattr(row, "cache_read_tokens", None),
        "cache_write_tokens": getattr(row, "cache_write_tokens", None),
        "error_type": row.error_type, "error_msg": row.error_msg,
        "fallback_count": row.fallback_count,
        "user_ip": row.user_ip, "api_key_id": row.api_key_id,
        "used_proxy": bool(row.used_proxy), "proxy_url": row.proxy_url,
        "request_body": req_body, "request_body_truncated": req_trunc,
        "response_body": resp_body, "response_body_truncated": resp_trunc,
        "archived_at": row.archived_at.isoformat() if getattr(row, "archived_at", None) else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
# ===================== 人工干预 =====================
class WeightsIn(BaseModel):
    w_speed: float
    w_intel: float
    w_stab: float
@router.get("/routing/weights")
async def get_routing_weights(db: AsyncSession = Depends(get_db)):
    """获取当前 auto 路由评分权重"""
    weights = await _rs.get_weights(db)
    return weights
@router.put("/routing/weights")
async def set_weights(data: WeightsIn, db: AsyncSession = Depends(get_db)):
    """调权重（和必须 = 1.0）"""
    total = data.w_speed + data.w_intel + data.w_stab
    if abs(total - 1.0) > 0.001:
        raise HTTPException(422, f"sum must be 1.0, got {total:.3f}")
    row = (await db.execute(select(RoutingWeights).where(RoutingWeights.id == 1))).scalar_one_or_none()
    if not row:
        row = RoutingWeights(id=1, w_speed=data.w_speed, w_intel=data.w_intel, w_stab=data.w_stab)
        db.add(row)
    else:
        row.w_speed = data.w_speed
        row.w_intel = data.w_intel
        row.w_stab = data.w_stab
    await db.commit()
    await _audit(db, "routing.weights.update", target_id=None,
                 payload={"w_speed": data.w_speed, "w_intel": data.w_intel, "w_stab": data.w_stab})
    return {"ok": True, "weights": {"w_speed": data.w_speed, "w_intel": data.w_intel, "w_stab": data.w_stab}}
@router.post("/models/{model_id}/cooldown")
async def set_cooldown(model_id: int, seconds: int = Query(..., ge=0, le=86400),
                        db: AsyncSession = Depends(get_db)):
    """手动冷却模型（seconds=0 解除）"""
    m = await db.get(Model, model_id)
    if not m:
        raise HTTPException(404, "Model not found")
    if seconds == 0:
        m.manual_cooldown_until = None
        await _audit(db, "model.cooldown.clear", target_id=model_id, payload={"seconds": 0})
    else:
        m.manual_cooldown_until = datetime.utcnow() + timedelta(seconds=seconds)
        await _audit(db, "model.cooldown.set", target_id=model_id, payload={"seconds": seconds})
    await db.commit()
    return {"ok": True, "model_id": model_id, "manual_cooldown_until":
            m.manual_cooldown_until.isoformat() if m.manual_cooldown_until else None}
class PinIn(BaseModel):
    model_id: Optional[int] = None
@router.put("/routing/pin")
async def set_pin(data: PinIn, db: AsyncSession = Depends(get_db)):
    """锁定模型（model_id=null 解除）"""
    if data.model_id is not None:
        m = await db.get(Model, data.model_id)
        if not m:
            raise HTTPException(404, "Model not found")
    row = (await db.execute(select(RoutingPin).where(RoutingPin.id == 1))).scalar_one_or_none()
    if not row:
        row = RoutingPin(id=1, pinned_model_id=data.model_id)
        db.add(row)
    else:
        row.pinned_model_id = data.model_id
    await db.commit()
    await _audit(db, "routing.pin", target_id=data.model_id,
                 payload={"model_id": data.model_id})
    return {"ok": True, "pinned_model_id": data.model_id}
# ===================== 审计 =====================
@router.get("/audit")
async def list_audit(limit: int = Query(50, ge=1, le=500),
                      action: Optional[str] = None,
                      db: AsyncSession = Depends(get_db)):
    q = select(AdminAuditLog).order_by(desc(AdminAuditLog.created_at)).limit(limit)
    if action:
        q = q.where(AdminAuditLog.action == action)
    rows = (await db.execute(q)).scalars().all()
    return [{
        "id": r.id, "actor": r.actor, "action": r.action,
        "target_id": r.target_id, "payload": r.payload,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
# ===================== 请求日志（分页） =====================
from sqlalchemy import func
# ===================== 日志列表 total 缓存（30s TTL，避免每次翻页全表 COUNT） =====================
_logs_total_cache = {"data": None, "ts": 0, "key": None}
_LOGS_TOTAL_TTL = 30  # 秒

def _logs_total_key(status: Optional[str], provider: Optional[str]) -> str:
    return f"{status or ''}|{provider or ''}"

@router.get("/logs")
async def list_request_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    status: Optional[str] = None,
    provider: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    base_q = select(RequestLog).where(RequestLog.is_health_check.is_(False)).options(
        defer(RequestLog.request_body), defer(RequestLog.response_body)
    )
    count_q = select(func.count(RequestLog.id)).where(RequestLog.is_health_check.is_(False))
    if status:
        base_q = base_q.where(RequestLog.status == status)
        count_q = count_q.where(RequestLog.status == status)
    if provider:
        base_q = base_q.where(RequestLog.routed_provider == provider)
        count_q = count_q.where(RequestLog.routed_provider == provider)
    # 方案B：total 缓存（30s TTL），翻页复用，省掉每次全表 COUNT
    now = _time.time()
    ck = _logs_total_key(status, provider)
    if _logs_total_cache["data"] is not None and _logs_total_cache["key"] == ck and (now - _logs_total_cache["ts"]) < _LOGS_TOTAL_TTL:
        total = _logs_total_cache["data"]
    else:
        total = (await db.execute(count_q)).scalar_one()
        _logs_total_cache.update({"data": total, "ts": now, "key": ck})
    rows = (await db.execute(
        base_q.order_by(desc(RequestLog.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all()
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "items": [{
            "id": r.id,
            "conversation_id": r.conversation_id,
            "requested_model": r.requested_model,
            "routed_provider": r.routed_provider,
            "routed_model": r.routed_model,
            "status": r.status,
            "media_type": getattr(r, "media_type", None),
            "latency_ms": r.latency_ms,
            "ttft_ms": getattr(r, "ttft_ms", None),
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "estimated_cost_usd": getattr(r, "estimated_cost_usd", None),
            "cache_read_tokens": getattr(r, "cache_read_tokens", None),
            "cache_write_tokens": getattr(r, "cache_write_tokens", None),
            "error_type": r.error_type,
            "error_msg": r.error_msg,
            "fallback_count": r.fallback_count,
            "user_ip": r.user_ip,
            "used_proxy": bool(r.used_proxy), "proxy_url": r.proxy_url,
            "archived": bool(getattr(r, "archived_at", None)),
            # request_body/response_body 不在列表接口返回，走 /logs/{id} 详情接口
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }
# ===================== 分析汇总 =====================
from server.models.health_check import HealthCheck
import time as _time

# 分析汇总缓存（30 秒 TTL，避免每次刷新都全表扫描）
_analytics_cache = {"data": None, "ts": 0}
_ANALYTICS_TTL = 30  # 秒

def _invalidate_analytics_cache():
    """归档/恢复/清空后让分析页重新查询"""
    global _analytics_cache
    _analytics_cache = {"data": None, "ts": 0}

@router.get("/analytics/summary")
async def analytics_summary(db: AsyncSession = Depends(get_db)):
    global _analytics_cache
    now = _time.time()
    if _analytics_cache["data"] and (now - _analytics_cache["ts"]) < _ANALYTICS_TTL:
        return _analytics_cache["data"]

    # 一条 SQL 搞定：COUNT、SUM 全部聚合（延迟/首字取 sum+count 以便与累计表合并算均值）
    row = (await db.execute(
        select(
            func.count(RequestLog.id),
            func.count(RequestLog.id).filter(RequestLog.status == "success"),
            func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
            func.coalesce(func.sum(RequestLog.completion_tokens), 0),
            func.coalesce(func.sum(RequestLog.latency_ms).filter(RequestLog.latency_ms.isnot(None)), 0),
            func.count(RequestLog.id).filter(RequestLog.latency_ms.isnot(None)),
            func.count(RequestLog.id).filter(RequestLog.requested_model == "auto"),
            func.coalesce(func.sum(RequestLog.ttft_ms).filter(RequestLog.ttft_ms.isnot(None)), 0),
            func.count(RequestLog.id).filter(RequestLog.ttft_ms.isnot(None)),
        ).where(RequestLog.is_health_check.is_(False))
    )).one()
    total, success_count, total_input, total_output, lat_sum, lat_cnt, auto_count, ttft_sum, ttft_cnt = row

    # 累计统计（归档后保留的部分）
    cum = (await db.execute(
        select(AnalyticsCumulative).where(AnalyticsCumulative.id == 1)
    )).scalar_one_or_none()
    if cum is not None:
        total = (total or 0) + cum.total_requests
        success_count = (success_count or 0) + cum.success_count
        total_input = (total_input or 0) + cum.total_input_tokens
        total_output = (total_output or 0) + cum.total_output_tokens
        lat_sum = (lat_sum or 0) + cum.sum_latency_ms
        lat_cnt = (lat_cnt or 0) + cum.latency_count
        auto_count = (auto_count or 0) + cum.auto_requests
        ttft_sum = (ttft_sum or 0) + (getattr(cum, "sum_ttft_ms", 0) or 0)
        ttft_cnt = (ttft_cnt or 0) + (getattr(cum, "ttft_count", 0) or 0)

    total = total or 0
    success_count = success_count or 0
    avg_lat = (lat_sum or 0) / (lat_cnt or 1) if lat_cnt else 0.0
    avg_ttft = (ttft_sum or 0) / ttft_cnt if ttft_cnt else None

    data = {
        "total_requests": total,
        "success_count": success_count,
        "success_rate": round(success_count / (total or 1) * 100, 1),
        "total_input_tokens": int(total_input or 0),
        "total_output_tokens": int(total_output or 0),
        "avg_latency_ms": round(avg_lat, 1),
        "avg_ttft_ms": round(avg_ttft, 1) if avg_ttft is not None else None,
        "ttft_samples": int(ttft_cnt or 0),
        "auto_requests": auto_count or 0,
        "direct_requests": total - (auto_count or 0),
    }
    _analytics_cache = {"data": data, "ts": now}
    return data


@router.post("/analytics/summary/reset")
async def reset_analytics_summary(db: AsyncSession = Depends(get_db)):
    """手动重置统计数据：清零累计统计表（当前 request_logs 实时日志不受影响）。

    归档后统计数据会保留在累计表中；如需重新统计，可点击「重置统计数据」清零。
    """
    await db.execute(text("UPDATE analytics_cumulative SET total_requests=0, success_count=0, "
                          "auto_requests=0, total_input_tokens=0, total_output_tokens=0, "
                          "sum_latency_ms=0, latency_count=0, "
                          "sum_ttft_ms=0, ttft_count=0 WHERE id=1"))
    await db.commit()
    _invalidate_analytics_cache()
    return {"ok": True, "message": "统计数据已重置，当前实时日志统计保留"}


# ===================== 用量分析（配额追踪并入） =====================
def _parse_dt_param(val, end_of_day: bool = False) -> Optional[datetime]:
    """宽松解析时间参数为 naive UTC：
    - 带 Z / ±HH:MM 时区的 ISO → 转 UTC 后去时区（前端 datetime-local 转换后传此格式）
    - 无时区的 YYYY-MM-DD / YYYY-MM-DDTHH:MM[:SS] → 视为 UTC（向后兼容）
    - date 形式可按 end_of_day 补足到当日 23:59:59
    """
    # FastAPI 未解析时可能传入 Query 默认对象（直接函数调用场景），非 str 一律视为未提供
    if not isinstance(val, str) or not val.strip():
        return None
    v = val.strip().replace(" ", "T")
    try:
        if len(v) == 10:
            d = datetime.fromisoformat(v)
            return d.replace(hour=23, minute=59, second=59, microsecond=0) if end_of_day else d
        if len(v) == 16:
            v += ":00"
        d = datetime.fromisoformat(v)
        if d.tzinfo is not None:
            from datetime import timezone as _tz
            d = d.astimezone(_tz.utc).replace(tzinfo=None)
        return d
    except ValueError:
        return None


@router.get("/analytics/summary/today")
async def analytics_today(
    start: Optional[str] = Query(None, description="起始时间 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM（UTC），默认今日零点"),
    end: Optional[str] = Query(None, description="结束时间（含），默认不限"),
    provider: Optional[str] = Query(None, description="按路由服务商过滤"),
    model: Optional[str] = Query(None, description="按路由模型名模糊过滤"),
    db: AsyncSession = Depends(get_db),
):
    """Token 用量汇总：请求数 / Token / 成本 / 成功率 / 缓存命中（数据源 request_logs）。

    无筛选参数时默认统计今日（UTC 零点起），与历史行为兼容。
    """
    start_dt = _parse_dt_param(start) or datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = _parse_dt_param(end, end_of_day=True)
    conditions = [RequestLog.is_health_check.is_(False), RequestLog.created_at >= start_dt]
    if end_dt:
        conditions.append(RequestLog.created_at <= end_dt)
    if isinstance(provider, str) and provider.strip():
        conditions.append(RequestLog.routed_provider == provider.strip())
    if isinstance(model, str) and model.strip():
        conditions.append(RequestLog.routed_model.like(f"%{model.strip()}%"))
    row = (await db.execute(
        select(
            func.count(RequestLog.id),
            func.count(RequestLog.id).filter(RequestLog.status == "success"),
            func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
            func.coalesce(func.sum(RequestLog.completion_tokens), 0),
            func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
            func.coalesce(func.sum(RequestLog.cache_read_tokens), 0),
        ).where(and_(*conditions))
    )).one()
    total, success, pin, pout, cost, crd = row
    total = int(total or 0)
    success = int(success or 0)
    pin = int(pin or 0)
    crd = int(crd or 0)
    return {
        "day": start_dt.strftime("%Y-%m-%d"),
        "range_start": start_dt.strftime("%Y-%m-%d %H:%M"),
        "range_end": end_dt.strftime("%Y-%m-%d %H:%M") if end_dt else None,
        "requests": total,
        "success_requests": success,
        "success_rate": round(success / (total or 1) * 100, 1),
        "prompt_tokens": pin,
        "completion_tokens": int(pout or 0),
        "total_tokens": pin + int(pout or 0),
        "cost_usd": round(float(cost or 0), 4),
        "cache_read_tokens": crd,
        "cache_hit_rate": round(crd / pin * 100, 1) if pin > 0 else None,
    }


@router.get("/analytics/trend")
async def analytics_trend(days: int = Query(7, ge=1, le=90), db: AsyncSession = Depends(get_db)):
    """最近 N 天每日趋势：请求数 / Token / 成本（数据源 request_logs）"""
    since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    # P2-11: 日聚合按方言选函数（SQLite: strftime / PostgreSQL: to_char）
    from server.db import IS_SQLITE as _is_sqlite
    if _is_sqlite:
        _day = func.strftime("%Y-%m-%d", RequestLog.created_at)
    else:
        _day = func.to_char(RequestLog.created_at, "YYYY-MM-DD")
    rows = (await db.execute(
        select(
            _day,
            func.count(RequestLog.id),
            func.coalesce(func.sum(RequestLog.prompt_tokens + RequestLog.completion_tokens), 0),
            func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
        ).where(RequestLog.created_at >= since)
        .group_by(_day)
        .order_by(_day)
    )).all()
    day_map = {str(r[0]): {"day": str(r[0]), "requests": int(r[1] or 0),
                           "tokens": int(r[2] or 0), "cost_usd": round(float(r[3] or 0), 4)} for r in rows}
    result = []
    for i in range(days):
        d = (since + timedelta(days=i)).strftime("%Y-%m-%d")
        result.append(day_map.get(d, {"day": d, "requests": 0, "tokens": 0, "cost_usd": 0.0}))
    return result


@router.get("/analytics/by-provider")
async def analytics_by_provider(db: AsyncSession = Depends(get_db)):
    """按服务商拆分今日用量（请求 / Token / 成本 / 占比%）"""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (await db.execute(
        select(
            RequestLog.routed_provider_id,
            func.count(RequestLog.id),
            func.coalesce(func.sum(RequestLog.prompt_tokens + RequestLog.completion_tokens), 0),
            func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
        ).where(RequestLog.created_at >= today_start, RequestLog.routed_provider_id.isnot(None))
        .group_by(RequestLog.routed_provider_id)
        .order_by(func.sum(RequestLog.prompt_tokens + RequestLog.completion_tokens).desc())
    )).all()
    items = []
    total_tokens = 0
    for r in rows:
        pid, reqs, toks, cost = r[0], int(r[1] or 0), int(r[2] or 0), round(float(r[3] or 0), 4)
        pname = None
        if pid:
            p = await db.get(Provider, pid)
            pname = p.name if p else None
        items.append({"provider_id": pid, "provider_name": pname or "(unknown)",
                      "requests": reqs, "tokens": toks, "cost_usd": cost})
        total_tokens += toks
    for it in items:
        it["share_pct"] = round(it["tokens"] / (total_tokens or 1) * 100, 1)
    items.sort(key=lambda x: x["tokens"], reverse=True)
    return {"total_tokens": total_tokens, "providers": items}


# ===================== 智力静态 CRUD =====================
class IntelIn(BaseModel):
    pattern: str
    score: int
    tier: str
    notes: Optional[str] = ""
@router.get("/intel-static")
async def list_intel(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(IntelligenceStatic).order_by(desc(IntelligenceStatic.score)))).scalars().all()
    return [{
        "id": r.id, "pattern": r.pattern, "score": r.score,
        "tier": r.tier, "notes": r.notes,
    } for r in rows]
@router.post("/intel-static")
async def upsert_intel(data: IntelIn, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(
        select(IntelligenceStatic).where(IntelligenceStatic.pattern == data.pattern)
    )).scalar_one_or_none()
    if existing:
        existing.score = data.score; existing.tier = data.tier; existing.notes = data.notes
        await _audit(db, "intel.update", target_id=existing.id, payload=data.model_dump())
    else:
        new = IntelligenceStatic(pattern=data.pattern, score=data.score, tier=data.tier, notes=data.notes)
        db.add(new)
        await db.flush()
        await _audit(db, "intel.create", target_id=new.id, payload=data.model_dump())
    await db.commit()
    return {"ok": True}

# ===================== 日志归档管理 =====================

# P1-8: 最近一次归档状态（内存态；归档是每天/手动的低频运维动作，重启清空可接受）
_last_archive_info = {"ok": None, "at": None, "count": 0, "blobs_deleted": 0,
                      "filename": "", "error": ""}


def _get_archive_dir() -> Path:
    cfg = get_config()
    d = Path(cfg.log_archive.archive_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_archive_meta(filename: str) -> dict:
    """从归档文件名和首行提取元信息，无需解压全部内容"""
    fpath = _get_archive_dir() / filename
    if not fpath.exists():
        return None
    try:
        with gzip.open(fpath, "rt", encoding="utf-8") as f:
            first = f.readline().strip()
        if first.startswith("{"):
            meta = json.loads(first)
            if "_meta" in meta:
                return {
                    "filename": filename,
                    "archived_at": meta["_meta"].get("archived_at", ""),
                    "date_from": meta["_meta"].get("date_from", ""),
                    "date_to": meta["_meta"].get("date_to", ""),
                    "count": meta["_meta"].get("count", 0),
                    "size_bytes": fpath.stat().st_size,
                }
    except Exception:
        pass
    return {
        "filename": filename,
        "archived_at": "",
        "date_from": "",
        "date_to": "",
        "count": 0,
        "size_bytes": fpath.stat().st_size,
    }




async def _do_archive(db: AsyncSession, target_date: str = None) -> dict:
    """核心归档逻辑：归档指定日期的日志，或 "all" 归档所有"""
    if target_date == "all":
        # 归档全部日志
        rows = (await db.execute(
            select(RequestLog).where(
                RequestLog.is_health_check.is_(False)
            ).order_by(RequestLog.created_at.asc())
        )).scalars().all()
        if not rows:
            return {"ok": True, "archived_count": 0, "message": "没有需要归档的日志"}
        date_from = rows[0].created_at.strftime("%Y-%m-%d")
        date_to = rows[-1].created_at.strftime("%Y-%m-%d")
        filename = f"arch-{date_from}--{date_to}.jsonl.gz"
    else:
        # 归档指定日期（默认昨天）
        if target_date is None:
            target_date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        day_start = datetime.strptime(target_date, "%Y-%m-%d")
        day_end = day_start + timedelta(days=1)
        rows = (await db.execute(
            select(RequestLog).where(
                RequestLog.is_health_check.is_(False),
                RequestLog.created_at >= day_start,
                RequestLog.created_at < day_end
            ).order_by(RequestLog.created_at.asc())
        )).scalars().all()
        if not rows:
            return {"ok": True, "archived_count": 0, "message": f"{target_date} 没有需要归档的日志"}
        date_from = target_date
        date_to = target_date
        filename = f"arch-{target_date}.jsonl.gz"
    ad = _get_archive_dir()
    fpath = ad / filename

    if fpath.exists():
        filename = f"arch-{date_from}--{date_to}-{datetime.utcnow().strftime('%H%M%S')}.jsonl.gz"
        fpath = ad / filename

    count = 0
    with gzip.open(fpath, "wt", encoding="utf-8") as f:
        meta = {
            "_meta": {
                "version": 2,
                "archived_at": datetime.utcnow().isoformat() + "Z",
                "date_from": date_from,
                "date_to": date_to,
                "count": len(rows),
                "mode": "slim",
            }
        }
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for r in rows:
            rec = {
                "id": r.id,
                "conversation_id": r.conversation_id,
                "requested_model": r.requested_model,
                "routed_provider": r.routed_provider,
                "routed_model": r.routed_model,
                "status": r.status,
                "http_status": r.http_status,
                "latency_ms": r.latency_ms,
                "ttft_ms": getattr(r, "ttft_ms", None),
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "cache_read_tokens": getattr(r, "cache_read_tokens", None),
                "cache_write_tokens": getattr(r, "cache_write_tokens", None),
                "error_type": r.error_type,
                "error_msg": r.error_msg,
                "fallback_count": r.fallback_count,
                "user_ip": r.user_ip,
                "api_key_id": r.api_key_id,
                "request_body": await reassemble_request(db, r),
                "response_body": await reassemble_response(db, r),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1

    # 归档瘦身：日志行保留（统计筛选继续可用），只清详细内容引用。
    # 统计不再累加 analytics_cumulative —— 行还在 DB 内，summary/today 自动涵盖历史，
    # 累加会造成双重计算；cumulative 保留旧值（代表更早已物理删除的行）。
    _now = datetime.utcnow()
    # 1) 先收集内容 blob 引用（清列之前，否则拿不到了）
    all_hashes = []
    for r in rows:
        if getattr(r, "request_env_hash", None):
            all_hashes.append(r.request_env_hash)
        mh = getattr(r, "request_msg_hashes", None)
        if mh and mh != "__raw__":
            try:
                all_hashes.extend(json.loads(mh))
            except Exception:
                pass
        elif mh == "__raw__" and getattr(r, "request_env_hash", None):
            pass  # 整包存储时 env hash 已收集
        if getattr(r, "response_body_hash", None):
            all_hashes.append(r.response_body_hash)
    # 2) 清空行的内容列，打归档标记（统计元数据保留）
    ids_to_archive = [r.id for r in rows]
    placeholders = ",".join([":" + str(i) for i in range(len(ids_to_archive))])
    params = {str(i): v for i, v in enumerate(ids_to_archive)}
    params["now"] = _now
    await db.execute(
        text(f"UPDATE request_logs SET request_body=NULL, response_body=NULL, "
             f"request_env_hash=NULL, request_msg_hashes=NULL, response_body_hash=NULL, "
             f"archived_at=:now WHERE id IN ({placeholders})"),
        params
    )
    # 3) 递减 blob 引用，归零的 blob 物理删除（共享 blob 由 ref_count 保护）
    from server.core.request_logger import release_blob_refs
    blobs_deleted = await release_blob_refs(db, all_hashes)
    # 4) 孤儿 GC 兜底：删除不被任何日志行引用的 blob（历史遗留/行写入失败等造成的
    #    ref_count>0 但无引用的孤儿，一次性全量回收）
    # P2-11: 方言无关实现——Python 侧收集全部引用 hash（不再依赖 SQLite json_each）
    ref_hashes = set()
    for env_h, msg_h, resp_h in (await db.execute(
        select(RequestLog.request_env_hash, RequestLog.request_msg_hashes, RequestLog.response_body_hash)
    )).all():
        if env_h:
            ref_hashes.add(env_h)
        if resp_h:
            ref_hashes.add(resp_h)
        if msg_h and msg_h != "__raw__":
            try:
                ref_hashes.update(json.loads(msg_h))
            except Exception:
                pass
    if ref_hashes:
        res_gc = await db.execute(text(
            "DELETE FROM log_msg_blobs WHERE hash NOT IN (" +
            ",".join(f"'{h}'" for h in ref_hashes) + ")"
        ))
    else:
        res_gc = await db.execute(text("DELETE FROM log_msg_blobs"))
    blobs_deleted += res_gc.rowcount or 0
    await db.commit()

    # VACUUM 回收空间（PG 无 VACUUM 语法，autovacuum 兜底）
    from server.db import IS_SQLITE as _is_sq
    if _is_sq:
        from server.db import engine as _engine
        async with _engine.begin() as conn:
            await conn.execute(text("VACUUM"))

    _invalidate_analytics_cache()
    _last_archive_info.update({
        "ok": True, "at": datetime.utcnow().isoformat()[:19],
        "count": count, "blobs_deleted": blobs_deleted, "filename": filename, "error": "",
    })
    return {
        "ok": True,
        "archived_count": count,
        "blobs_deleted": blobs_deleted,
        "mode": "slim",
        "filename": filename,
        "date_range": f"{date_from} ~ {date_to}",
        "size_bytes": fpath.stat().st_size,
    }


@router.post("/logs/archive")
async def trigger_archive(date: str = Query(None, description="归档日期 YYYY-MM-DD，或 'all' 归档全部，默认全部"), db: AsyncSession = Depends(get_db)):
    """手动触发归档：默认归档全部日志，可指定日期"""
    try:
        return await _do_archive(db, date or "all")
    except Exception as e:
        _last_archive_info.update({
            "ok": False, "at": datetime.utcnow().isoformat()[:19],
            "count": 0, "blobs_deleted": 0, "filename": "", "error": str(e)[:300],
        })
        raise


@router.post("/logs/archives/{filename}/restore")
async def restore_archive(filename: str, db: AsyncSession = Depends(get_db)):
    """恢复归档：v2 瘦身归档 → 把详细内容重新挂回仍在 DB 的日志行；
    旧版归档（行已删除）→ 重新插入整行。全部成功才删除归档文件。"""
    fpath = _get_archive_dir() / filename
    if not fpath.exists():
        raise HTTPException(404, f"归档文件不存在: {filename}")

    records = []
    try:
        with gzip.open(fpath, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "_meta" in rec:
                    continue  # 跳过元信息行
                records.append(rec)
    except Exception as e:
        raise HTTPException(500, f"读取归档文件失败: {e}")

    if not records:
        raise HTTPException(400, "归档文件为空")

    from server.core.request_logger import _store_request, _store_response
    from sqlalchemy import text as sa_text
    relinked = 0
    inserted = 0
    failed = 0
    for rec in records:
        try:
            existing = await db.get(RequestLog, rec.get("id"))
            if existing is not None:
                # v2 瘦身归档：行还在，把详细内容重新去重入库并挂回引用
                rb = rec.get("request_body")
                resp = rec.get("response_body")
                env_hash, msg_hashes = (None, None)
                if rb:
                    env_hash, msg_hashes = await _store_request(db, rb)
                resp_hash = await _store_response(db, resp) if resp else None
                existing.request_body = None
                existing.response_body = None
                existing.request_env_hash = env_hash
                existing.request_msg_hashes = msg_hashes
                existing.response_body_hash = resp_hash
                existing.archived_at = None
                relinked += 1
                continue
            # 旧版归档：行已被物理删除，重新插入
            if rec.get("created_at") and isinstance(rec["created_at"], str):
                rec["created_at"] = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
            rec.setdefault("cache_read_tokens", None)
            rec.setdefault("cache_write_tokens", None)
            rec.setdefault("ttft_ms", None)
            await db.execute(sa_text("""
                INSERT INTO request_logs
                    (id, conversation_id, requested_model, routed_provider, routed_model,
                     status, http_status, latency_ms, ttft_ms, prompt_tokens, completion_tokens,
                     cache_read_tokens, cache_write_tokens,
                     error_type, error_msg, fallback_count, user_ip, api_key_id,
                     request_body, response_body, created_at)
                VALUES
                    (:id, :conversation_id, :requested_model, :routed_provider, :routed_model,
                     :status, :http_status, :latency_ms, :ttft_ms, :prompt_tokens, :completion_tokens,
                     :cache_read_tokens, :cache_write_tokens,
                     :error_type, :error_msg, :fallback_count, :user_ip, :api_key_id,
                     :request_body, :response_body, :created_at)
            """), rec)
            inserted += 1
        except Exception:
            failed += 1

    await db.commit()

    # 全部成功才删除归档文件；有失败的保留文件便于重试
    ok_all = (relinked + inserted) == len(records) and failed == 0
    if ok_all:
        fpath.unlink()

    _invalidate_analytics_cache()
    return {
        "ok": failed == 0,
        "relinked_count": relinked,
        "inserted_count": inserted,
        "failed_count": failed,
        "total_in_archive": len(records),
        "message": (f"成功恢复 {relinked + inserted} 条（挂回 {relinked} 条、重建 {inserted} 条，失败 {failed} 条）"
                    + ("，归档文件已删除" if ok_all else "；存在失败项，归档文件已保留")),
    }


@router.delete("/logs/archives/{filename}")
async def delete_archive(filename: str):
    """删除归档文件"""
    fpath = _get_archive_dir() / filename
    if not fpath.exists():
        raise HTTPException(404, f"归档文件不存在: {filename}")
    fpath.unlink()
    return {"ok": True, "message": f"归档文件 {filename} 已删除"}


@router.delete("/logs")
async def clear_logs(db: AsyncSession = Depends(get_db)):
    """清空所有当前请求日志（保留健康探测记录）"""
    result = await db.execute(
        text("SELECT COUNT(*) FROM request_logs WHERE conversation_id NOT LIKE 'hc-%'")
    )
    count = result.scalar_one()

    if count == 0:
        return {"ok": True, "deleted": 0, "message": "没有可删除的日志"}

    # 清空前把统计累加到累计表（与归档一致，统计不清零；需要清零点「重置统计数据」）
    row = (await db.execute(text(
        "SELECT COUNT(*), "
        "COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN requested_model='auto' THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
        "COALESCE(SUM(latency_ms),0), "
        "COALESCE(SUM(CASE WHEN latency_ms IS NOT NULL THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(ttft_ms),0), "
        "COALESCE(SUM(CASE WHEN ttft_ms IS NOT NULL THEN 1 ELSE 0 END),0) "
        "FROM request_logs WHERE conversation_id NOT LIKE 'hc-%'"
    ))).one()
    t, s, a, i, o, ls, lc, ts, tc = row
    await db.execute(text(
        "UPDATE analytics_cumulative SET "
        "total_requests=total_requests+:t, success_count=success_count+:s, "
        "auto_requests=auto_requests+:a, total_input_tokens=total_input_tokens+:i, "
        "total_output_tokens=total_output_tokens+:o, sum_latency_ms=sum_latency_ms+:ls, "
        "latency_count=latency_count+:lc, "
        "sum_ttft_ms=sum_ttft_ms+:ts, ttft_count=ttft_count+:tc WHERE id=1"
    ), {"t": t, "s": s, "a": a, "i": i, "o": o, "ls": ls, "lc": lc, "ts": ts, "tc": tc})

    await db.execute(
        text("DELETE FROM request_logs WHERE conversation_id NOT LIKE 'hc-%'")
    )
    await db.commit()

    # VACUUM 回收空间（PG 无 VACUUM 语法，autovacuum 兜底）
    from server.db import IS_SQLITE as _is_sq
    if _is_sq:
        from server.db import engine as _engine
        async with _engine.begin() as conn:
            await conn.execute(text("VACUUM"))

    _invalidate_analytics_cache()
    return {"ok": True, "deleted": count, "message": f"已清空 {count} 条日志并回收磁盘空间"}
