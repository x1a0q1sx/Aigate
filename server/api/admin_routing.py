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
from sqlalchemy import select, text, desc
from server.db import AsyncSessionLocal
from server.models.request_log import RequestLog
from server.models.intelligence import IntelligenceStatic
from server.models.routing_config import RoutingWeights, RoutingPin, AdminAuditLog
from server.models.model import Model
from server.models.provider import Provider
from server.core.ranking_service import RankingService
from server.core.health_checker import HealthChecker
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
async def overall_ranking(limit: int = Query(20, ge=1, le=500),
                            db: AsyncSession = Depends(get_db)):
    """综合评分榜（含权重、三维分项）"""
    models = (await db.execute(
        select(Model, Provider).join(Provider, Model.provider_id == Provider.id)
        .where(Model.enabled == True)
    )).all()
    mlist = [m for m, _ in models]
    prov_by_pid = {}
    for m, p in models:
        prov_by_pid[m.provider_id] = p
    weights = await _rs.get_weights(db)
    cooling = {}
    hc = HealthChecker()
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
            "p50_ms": s.p50_ms,
            "success_rate": s.success_rate,
            "final_score": s.final_score,
            "excluded_reason": s.excluded_reason,
            "priority_boost": s.priority_boost,
            "cooldown_until": cd_until,
            "fail_count": fc,
            "weights": weights,
        })
    return {"weights": weights, "ranking": out}
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
    return {"archives": archives}

@router.get("/logs/{log_id}")
async def get_log(log_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(RequestLog, log_id)
    if not row:
        raise HTTPException(404, "Log not found")
    return {
        "id": row.id, "conversation_id": row.conversation_id,
        "requested_model": row.requested_model,
        "routed_provider": row.routed_provider, "routed_model": row.routed_model,
        "status": row.status, "http_status": row.http_status,
        "latency_ms": row.latency_ms,
        "prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens,
        "error_type": row.error_type, "error_msg": row.error_msg,
        "fallback_count": row.fallback_count,
        "user_ip": row.user_ip, "api_key_id": row.api_key_id,
        "request_body": row.request_body,
        "response_body": row.response_body,
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
@router.get("/logs")
async def list_request_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    base_q = select(RequestLog).where(RequestLog.conversation_id.not_like("hc-%"))
    count_q = select(func.count(RequestLog.id)).where(RequestLog.conversation_id.not_like("hc-%"))
    if status:
        base_q = base_q.where(RequestLog.status == status)
        count_q = count_q.where(RequestLog.status == status)
    total = (await db.execute(count_q)).scalar_one()
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
            "latency_ms": r.latency_ms,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "error_type": r.error_type,
            "error_msg": r.error_msg,
            "fallback_count": r.fallback_count,
            "user_ip": r.user_ip,
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

    # 一条 SQL 搞定：COUNT、SUM、AVG 全部聚合
    row = (await db.execute(
        select(
            func.count(RequestLog.id),
            func.count(RequestLog.id).filter(RequestLog.status == "success"),
            func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
            func.coalesce(func.sum(RequestLog.completion_tokens), 0),
            func.coalesce(func.avg(RequestLog.latency_ms).filter(RequestLog.latency_ms.isnot(None)), 0),
            func.count(RequestLog.id).filter(RequestLog.requested_model == "auto"),
        ).where(RequestLog.conversation_id.not_like("hc-%"))
    )).one()
    total, success_count, total_input, total_output, avg_lat, auto_count = row

    data = {
        "total_requests": total or 0,
        "success_count": success_count or 0,
        "success_rate": round((success_count or 0) / (total or 1) * 100, 1),
        "total_input_tokens": int(total_input or 0),
        "total_output_tokens": int(total_output or 0),
        "avg_latency_ms": round(avg_lat or 0, 1),
        "auto_requests": auto_count or 0,
        "direct_requests": (total or 0) - (auto_count or 0),
    }
    _analytics_cache = {"data": data, "ts": now}
    return data
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
                RequestLog.conversation_id.not_like("hc-%")
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
                RequestLog.conversation_id.not_like("hc-%"),
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
                "version": 1,
                "archived_at": datetime.utcnow().isoformat() + "Z",
                "date_from": date_from,
                "date_to": date_to,
                "count": len(rows),
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
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "error_type": r.error_type,
                "error_msg": r.error_msg,
                "fallback_count": r.fallback_count,
                "user_ip": r.user_ip,
                "api_key_id": r.api_key_id,
                "request_body": r.request_body,
                "response_body": r.response_body,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1

    # 从 DB 删除已归档记录
    ids_to_delete = [r.id for r in rows]
    placeholders = ",".join([":" + str(i) for i in range(len(ids_to_delete))])
    params = {str(i): v for i, v in enumerate(ids_to_delete)}
    await db.execute(
        text(f"DELETE FROM request_logs WHERE id IN ({placeholders})"),
        params
    )
    await db.commit()

    # VACUUM 回收空间
    from server.db import engine as _engine
    async with _engine.begin() as conn:
        await conn.execute(text("VACUUM"))

    _invalidate_analytics_cache()
    return {
        "ok": True,
        "archived_count": count,
        "filename": filename,
        "date_range": f"{date_from} ~ {date_to}",
        "size_bytes": fpath.stat().st_size,
    }


@router.post("/logs/archive")
async def trigger_archive(date: str = Query(None, description="归档日期 YYYY-MM-DD，或 'all' 归档全部，默认全部"), db: AsyncSession = Depends(get_db)):
    """手动触发归档：默认归档全部日志，可指定日期"""
    return await _do_archive(db, date or "all")


@router.post("/logs/archives/{filename}/restore")
async def restore_archive(filename: str, db: AsyncSession = Depends(get_db)):
    """恢复归档：将归档文件解压并重新导入 DB，然后删除归档文件"""
    fpath = _get_archive_dir() / filename
    if not fpath.exists():
        raise HTTPException(404, f"归档文件不存在: {filename}")

    records = []
    restored_count = 0
    import_success = 0

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

    # 批量插入
    from sqlalchemy import text as sa_text
    import_success = 0
    import_failed = 0
    for rec in records:
        try:
            # JSON 中的 created_at 是 ISO 字符串，DB 需要 datetime
            if rec.get("created_at") and isinstance(rec["created_at"], str):
                rec["created_at"] = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
            await db.execute(sa_text("""
                INSERT INTO request_logs
                    (id, conversation_id, requested_model, routed_provider, routed_model,
                     status, http_status, latency_ms, prompt_tokens, completion_tokens,
                     error_type, error_msg, fallback_count, user_ip, api_key_id,
                     request_body, response_body, created_at)
                VALUES
                    (:id, :conversation_id, :requested_model, :routed_provider, :routed_model,
                     :status, :http_status, :latency_ms, :prompt_tokens, :completion_tokens,
                     :error_type, :error_msg, :fallback_count, :user_ip, :api_key_id,
                     :request_body, :response_body, :created_at)
            """), rec)
            import_success += 1
        except Exception as e:
            import_failed += 1

    await db.commit()

    # 删除归档文件
    fpath.unlink()

    _invalidate_analytics_cache()
    return {
        "ok": True,
        "restored_count": import_success,
        "failed_count": import_failed,
        "total_in_archive": len(records),
        "message": f"成功恢复 {import_success} 条（失败 {import_failed} 条），归档文件已删除",
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

    await db.execute(
        text("DELETE FROM request_logs WHERE conversation_id NOT LIKE 'hc-%'")
    )
    await db.commit()

    # VACUUM 回收空间
    from server.db import engine as _engine
    async with _engine.begin() as conn:
        await conn.execute(text("VACUUM"))

    _invalidate_analytics_cache()
    return {"ok": True, "deleted": count, "message": f"已清空 {count} 条日志并回收磁盘空间"}
