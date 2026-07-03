"""
/admin/api/* 管理 API
v2.0: 新增手动测速 API
"""
from typing import Optional
from datetime import datetime, timezone
import httpx
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from server.db import AsyncSessionLocal
from server.models.provider import Provider
from server.models.api_key import ApiKey
from server.models.model import Model
from server.models.health_check import HealthCheck
from server.models.request_log import RequestLog
from server.schemas.provider import (
    ProviderCreate, ProviderUpdate, ProviderResponse,
    ApiKeyCreate, ApiKeyResponse,
    ModelUpdate, ModelInfoResponse, ModelsRefreshResponse,
    PingResult, PingAllResponse, LatencyStatsResponse
)
from server.schemas.admin import (
    DashboardSummary, HealthStatusResponse, HealthStatusItem,
    PlaygroundRequest
)
from server.core.key_manager import KeyManager
from server.core.model_catalog import ModelCatalog
from server.core.health_checker import HealthChecker
from server.core.crypto_service import get_crypto_service
from server.config import get_config, save_config
from sqlalchemy import select, func, desc
router = APIRouter(prefix="/admin/api")

def utc_iso(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
config = get_config()
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
# 实例化服务
_key_manager = KeyManager(get_crypto_service())
_model_catalog = ModelCatalog()
@router.get("/providers")
async def list_providers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Provider).order_by(Provider.id))
    providers = list(result.scalars().all())
    return [ProviderResponse.model_validate(p) for p in providers]
@router.post("/providers")
async def create_provider(data: ProviderCreate, db: AsyncSession = Depends(get_db)):
    provider = Provider(
        name=data.name,
        base_url=data.base_url,
        api_type=data.api_type,
        headers=data.headers or {},
        description=data.description or ""
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return ProviderResponse.model_validate(provider)
@router.put("/providers/{provider_id}")
async def update_provider(provider_id: int, data: ProviderUpdate, db: AsyncSession = Depends(get_db)):
    provider = await db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if data.name is not None:
        provider.name = data.name
    if data.base_url is not None:
        provider.base_url = data.base_url
    if data.api_type is not None:
        provider.api_type = data.api_type
    if data.headers is not None:
        provider.headers = data.headers
    if data.description is not None:
        provider.description = data.description
    await db.commit()
    await db.refresh(provider)
    return ProviderResponse.model_validate(provider)
@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    provider = await db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    await db.delete(provider)
    await db.commit()
    return {"ok": True}
@router.get("/keys")
async def list_keys(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    keys = list(result.scalars().all())
    return [ApiKeyResponse.model_validate(k) for k in keys]

@router.get("/keys/{key_id}/reveal")
async def reveal_key(key_id: int, db: AsyncSession = Depends(get_db)):
    plaintext = await _key_manager.decrypt_key(db, key_id)
    if plaintext is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"id": key_id, "key": plaintext}

@router.get("/aigate-key")
async def get_aigate_key(reveal: bool = False):
    key = getattr(config.security, "aigate_api_key", "") or ""
    if not key:
        return {"configured": False, "masked": "", "key": "" if reveal else None}
    masked = _key_manager.mask_key(key)
    return {
        "configured": True,
        "masked": masked,
        "key": key if reveal else None,
    }
@router.post("/keys")
async def add_key(data: ApiKeyCreate, db: AsyncSession = Depends(get_db)):
    provider = await db.get(Provider, data.provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    key = await _key_manager.add_key(db, data.provider_id, data.key, data.label or "")
    return ApiKeyResponse.model_validate(key)
@router.delete("/keys/{key_id}")
async def delete_key(key_id: int, db: AsyncSession = Depends(get_db)):
    success = await _key_manager.delete_key(db, key_id)
    if not success:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"ok": True}
@router.get("/models")
async def list_models(
    provider_id: Optional[int] = None,
    is_free: Optional[bool] = None,
    auto_enabled: Optional[bool] = None,
    q: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """列出模型，附带最新延迟信息"""
    from server.main import get_health_checker
    hc = get_health_checker()
    from server.models.provider import Provider as ProvModel
    models = await _model_catalog.list_models(db, provider_id, is_free, auto_enabled)
    # 预加载 provider 关系
    for m in models:
        provider = await db.get(ProvModel, m.provider_id)
        m.provider = provider if provider else None
    result = []
    if q:
        needle = q.strip().lower()
        models = [m for m in models if needle in (m.model_id or "").lower() or needle in (m.display_name or "").lower() or needle in ((m.provider.name if getattr(m, "provider", None) else "") or "").lower()]
    for m in models:
        item = ModelInfoResponse.from_orm(m)
        # 附加延迟 + 冷却信息
        if hc:
            cached = hc.get_cached_status(m.id)
            if cached:
                item.latency_ms = cached.latency_ms
                item.health_status = cached.status
            # 冷却信息
            item.fail_count = hc._fail_count.get(m.id, 0)
            cd = hc._cooling.get(m.id)
            if cd and datetime.now(timezone.utc).replace(tzinfo=None) < cd:
                item.cooldown_until = cd.isoformat() + "Z"
        result.append(item)
    return result
@router.put("/models/{model_id}")
async def update_model(model_id: int, data: ModelUpdate, db: AsyncSession = Depends(get_db)):
    model = await _model_catalog.update_model(
        db, model_id,
        auto_enabled=data.auto_enabled,
        enabled=data.enabled,
        input_price=data.input_price,
        output_price=data.output_price,
        success_rate=data.success_rate,
        is_free=data.is_free,
        priority_boost=data.priority_boost,
        auto_excluded=data.auto_excluded
    )
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return ModelInfoResponse.from_orm(model)
@router.delete("/models/{model_id}")
async def delete_model(model_id: int, db: AsyncSession = Depends(get_db)):
    """删除指定模型"""
    from sqlalchemy import delete as sqldelete
    result = await db.execute(sqldelete(Model).where(Model.id == model_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Model not found")
    await db.commit()
    return {"ok": True, "deleted": model_id}
@router.post("/models/refresh")
async def refresh_models(
    provider_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db)
):
    """刷新模型列表，如果 provider_id 指定则只刷新该服务商"""
    if provider_id:
        provider = await db.get(Provider, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        providers = [provider]
    else:
        # 刷新所有有密钥的 provider
        result = await db.execute(
            select(Provider).join(ApiKey, Provider.id == ApiKey.provider_id).distinct()
        )
        providers = list(result.scalars().all())
    total_added = 0
    total_updated = 0
    total_total = 0
    total_pricing_updated = 0
    total_metric_updated = 0
    pricing_sources = []
    for provider in providers:
        result = await _model_catalog.refresh_models_from_provider(db, provider, _key_manager)
        if "error" in result:
            continue
        total_added += result.get("added", 0)
        total_updated += result.get("updated", 0)
        total_total += result.get("total", 0)
        total_pricing_updated += result.get("pricing_updated", 0)
        total_metric_updated += result.get("metric_updated", 0)
        source = result.get("pricing_source")
        if source and source not in pricing_sources:
            pricing_sources.append(source)
    return ModelsRefreshResponse(
        added=total_added,
        updated=total_updated,
        total=total_total,
        pricing_updated=total_pricing_updated,
        metric_updated=total_metric_updated,
        pricing_sources=pricing_sources,
    )
# ========== 手动添加模型 ==========
class ManualModelAdd(BaseModel):
    model_id: str
    display_name: str = ""
    input_price: float = 0.0
    output_price: float = 0.0

@router.post("/providers/{provider_id}/models")
async def add_model_to_provider(provider_id: int, data: ManualModelAdd, db: AsyncSession = Depends(get_db)):
    """手动为指定服务商添加模型"""
    provider = await db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    existing = (await db.execute(
        select(Model).where(Model.provider_id == provider_id, Model.model_id == data.model_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Model '{data.model_id}' already exists")
    now = datetime.utcnow()
    model = Model(
        provider_id=provider_id,
        model_id=data.model_id,
        display_name=data.display_name or data.model_id,
        input_price=data.input_price,
        output_price=data.output_price,
        is_free=(data.input_price == 0.0 and data.output_price == 0.0),
        enabled=True,
        auto_enabled=True,
        supports_streaming=True,
        context_length=4096,
        created_at=now,
    )
    db.add(model)
    await db.commit()
    return {"id": model.id, "model_id": model.model_id, "added": True}

# ========== 导入定价 JSON ==========
class PricingImport(BaseModel):
    json_data: str  # 原始 JSON 字符串

@router.post("/providers/{provider_id}/import-pricing")
async def import_provider_pricing(provider_id: int, data: PricingImport, db: AsyncSession = Depends(get_db)):
    """导入 newapi/one-api 格式的 /api/pricing 返回数据，自动匹配模型并更新定价"""
    provider = await db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    try:
        import json as _j
        raw = _j.loads(data.json_data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    items = raw.get("data", []) if isinstance(raw, dict) else []
    if not items:
        raise HTTPException(status_code=400, detail="No 'data' array found in JSON")
    
    group_ratio = (raw.get("group_ratio", {}) or {}).get("default", 1) if isinstance(raw, dict) else 1
    
    updated = 0
    created = 0
    now = datetime.utcnow()
    for item in items:
        name = item.get("model_name", item.get("model", ""))
        if not name:
            continue
        mr = float(item.get("model_ratio", item.get("model_price", 0)) or 0)
        cr = float(item.get("completion_ratio", 1) or 1)
        mp = float(item.get("model_price", 0) or 0)
        
        if mp > 0:
            inp = round(mp * mr * group_ratio, 6)
            out = round(mp * mr * cr * group_ratio, 6)
        elif mr > 0:
            inp = round(mr * group_ratio, 6)
            out = round(mr * cr * group_ratio, 6)
        else:
            continue
        
        # 更新匹配的模型
        result = await db.execute(
            select(Model).where(Model.provider_id == provider_id, Model.model_id == name)
        )
        model = result.scalar_one_or_none()
        if model:
            model.input_price = inp
            model.output_price = out
            model.is_free = (inp == 0 and out == 0)
            if hasattr(model, 'pricing_source'):
                model.pricing_source = provider.base_url
            updated += 1
        else:
            # 创建不存在的模型
            model = Model(
                provider_id=provider_id,
                model_id=name,
                display_name=name,
                input_price=inp,
                output_price=out,
                is_free=(inp == 0 and out == 0),
                enabled=True,
                auto_enabled=False,
                supports_streaming=True,
                context_length=4096,
                created_at=now,
            )
            db.add(model)
            created += 1
        if model:
            model.input_price = inp
            model.output_price = out
            model.is_free = (inp == 0 and out == 0)
            if hasattr(model, 'pricing_source'):
                model.pricing_source = provider.base_url
            updated += 1
    
    await db.commit()
    return {"updated": updated, "created": created, "total_models": len(items), "group_ratio": group_ratio}

# ========== v2.0 新增：手动测速 API ==========
@router.post("/models/{model_id}/ping", response_model=PingResult)
async def ping_single_model(model_id: int, db: AsyncSession = Depends(get_db)):
    """手动探测单个模型延迟"""
    from server.main import get_health_checker
    hc = get_health_checker()
    model = await db.get(Model, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    provider = await db.get(Provider, model.provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    # 获取 key
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.provider_id == provider.id, ApiKey.is_active == True)
        .limit(1)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=400, detail="No active API key for this provider")
    # 执行探测
    health_result = await hc.check_model(db, model, provider, _key_manager)
    return PingResult(
        model_id=model.id,
        model_full_id=f"{provider.name}/{model.model_id}",
        status=health_result.status,
        latency_ms=health_result.latency_ms,
        error_message=health_result.error_message,
        checked_at=utc_iso(datetime.utcnow())
    )
@router.post("/models/ping-all", response_model=PingAllResponse)
async def ping_all_models(db: AsyncSession = Depends(get_db)):
    """批量探测参与 auto 的已启用模型"""
    from server.main import get_health_checker
    import asyncio
    hc = get_health_checker()
    # 只批量探测参与 auto 的已启用模型，避免未选择 auto 的收费模型被误探测
    result = await db.execute(
        select(Model)
        .join(Provider, Model.provider_id == Provider.id)
        .where(
            Model.enabled == True,
            Model.auto_enabled == True,
            Model.auto_excluded == False
        )
    )
    models = list(result.scalars().all())
    if not models:
        return PingAllResponse(
            total=0, healthy=0, degraded=0, rate_limited=0, unhealthy=0, results=[]
        )
    results = []
    stats = {"healthy": 0, "degraded": 0, "rate_limited": 0, "unhealthy": 0}
    for model in models:
        provider = await db.get(Provider, model.provider_id)
        try:
            health_result = await hc.check_model(db, model, provider, _key_manager)
            stats[health_result.status] = stats.get(health_result.status, 0) + 1
            results.append(PingResult(
                model_id=model.id,
                model_full_id=f"{provider.name}/{model.model_id}" if provider else str(model.model_id),
                status=health_result.status,
                latency_ms=health_result.latency_ms,
                error_message=health_result.error_message,
                checked_at=datetime.utcnow().isoformat()
            ))
        except Exception as e:
            stats["unhealthy"] += 1
            results.append(PingResult(
                model_id=model.id,
                model_full_id=f"{provider.name}/{model.model_id}" if provider else str(model.model_id),
                status="unhealthy",
                latency_ms=0,
                error_message=str(e),
                checked_at=datetime.utcnow().isoformat()
            ))
        # 避免请求太密集
        await asyncio.sleep(0.5)
    return PingAllResponse(
        total=len(models),
        healthy=stats.get("healthy", 0),
        degraded=stats.get("degraded", 0),
        rate_limited=stats.get("rate_limited", 0),
        unhealthy=stats.get("unhealthy", 0),
        results=results
    )
@router.get("/models/latency-stats", response_model=LatencyStatsResponse)
async def get_latency_stats(db: AsyncSession = Depends(get_db)):
    """获取延迟统计（最快/最慢/平均）"""
    from server.main import get_health_checker
    hc = get_health_checker()
    # 获取所有有健康检查记录的模型
    models = await _model_catalog.list_models(db, enabled_only=True)
    model_latencies = []
    for m in models:
        provider = await db.get(Provider, m.provider_id)
        cached = hc.get_cached_status(m.id) if hc else None
        latency = cached.latency_ms if cached and cached.latency_ms else None
        status = cached.status if cached else "unknown"
        if latency is not None and latency > 0:
            model_latencies.append({
                "model_id": m.id,
                "model_full_id": f"{provider.name}/{m.model_id}" if provider else m.model_id,
                "latency_ms": latency,
                "status": status
            })
    # 按延迟排序
    model_latencies.sort(key=lambda x: x["latency_ms"])
    fastest = [
        PingResult(
            model_id=m["model_id"],
            model_full_id=m["model_full_id"],
            status=m["status"],
            latency_ms=m["latency_ms"],
            checked_at=datetime.utcnow().isoformat()
        )
        for m in model_latencies[:5]
    ]
    slowest = [
        PingResult(
            model_id=m["model_id"],
            model_full_id=m["model_full_id"],
            status=m["status"],
            latency_ms=m["latency_ms"],
            checked_at=datetime.utcnow().isoformat()
        )
        for m in model_latencies[-5:][::-1]  # 反转使最慢的排前面
    ]
    avg_latency = (
        sum(m["latency_ms"] for m in model_latencies) / len(model_latencies)
        if model_latencies else 0
    )
    return LatencyStatsResponse(
        fastest=fastest,
        slowest=slowest,
        average_latency_ms=round(avg_latency, 1),
        total_models=len(model_latencies)
    )
# ========== 原有 API 继续 ==========
@router.get("/health")
async def get_health(
    model_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db)
):
    """获取健康状态：只看 auto 模型，全局最近 20 条探测记录"""
    auto_ids_subq = select(Model.id).where(Model.enabled == True, Model.auto_enabled == True, Model.auto_excluded == False)
    # 清理非 auto 模型的旧探测记录
    old_checks = await db.execute(
        select(HealthCheck).where(~HealthCheck.model_id.in_(auto_ids_subq))
    )
    for old in old_checks.scalars().all():
        await db.delete(old)
    # 全局只保留最近 20 条（跨所有 auto 模型）
    all_checks = (await db.execute(
        select(HealthCheck).where(HealthCheck.model_id.in_(auto_ids_subq)).order_by(desc(HealthCheck.checked_at))
    )).scalars().all()
    if len(all_checks) > 20:
        for old in all_checks[20:]:
            await db.delete(old)
    await db.commit()
    if model_id:
        query = (
            select(HealthCheck)
            .where(HealthCheck.model_id == model_id, HealthCheck.model_id.in_(auto_ids_subq))
            .order_by(desc(HealthCheck.checked_at))
            .limit(20)
        )
    else:
        query = (
            select(HealthCheck)
            .where(HealthCheck.model_id.in_(auto_ids_subq))
            .order_by(desc(HealthCheck.checked_at))
            .limit(20)
        )
    result = await db.execute(query)
    checks = list(result.scalars().all())
    items = []
    for check in checks:
        model = await db.get(Model, check.model_id)
        provider = await db.get(Provider, model.provider_id) if model else None
        model_full_id = f"{provider.name}/{model.model_id}" if provider and model else str(check.model_id)
        items.append(HealthStatusItem(
            model_id=check.model_id,
            model_full_id=model_full_id,
            status=check.status,
            latency_ms=check.latency_ms,
            last_checked=utc_iso(check.checked_at),
            error_message=check.error_message
        ))
    return HealthStatusResponse(items=items)
@router.get("/dashboard")
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    """仪表盘汇总"""
    from server.main import get_health_checker
    hc = get_health_checker()
    statuses = hc.get_all_cached_status()
    counts = {"healthy": 0, "degraded": 0, "rate_limited": 0, "unhealthy": 0}
    for s in statuses.values():
        counts[s.status] = counts.get(s.status, 0) + 1
    total_providers_result = await db.execute(select(func.count(Provider.id)))
    total_providers = total_providers_result.scalar_one() or 0
    total_keys_result = await db.execute(select(func.count(ApiKey.id)))
    total_keys = total_keys_result.scalar_one() or 0
    total_models_result = await db.execute(
        select(func.count(Model.id)).where(Model.enabled == True)
    )
    total_models = total_models_result.scalar_one() or 0
    auto_candidates_result = await db.execute(
        select(func.count(Model.id)).where(Model.enabled == True, Model.auto_enabled == True)
    )
    auto_candidates = auto_candidates_result.scalar_one() or 0
    return DashboardSummary(
        total_providers=total_providers,
        total_keys=total_keys,
        total_models=total_models,
        auto_candidates=auto_candidates,
        healthy_models=counts.get("healthy", 0),
        degraded_models=counts.get("degraded", 0),
        rate_limited_models=counts.get("rate_limited", 0),
        unhealthy_models=counts.get("unhealthy", 0)
    )
# ========== 当前 auto 模型 ==========
@router.get("/current-model")
async def get_current_model(db: AsyncSession = Depends(get_db)):
    """返回 auto 排名第一的模型（非历史最新使用）"""
    try:
        from server.core.ranking_service import RankingService
        from server.core.health_checker import HealthChecker
        rs = RankingService()
        models = (await db.execute(
            select(Model, Provider).join(Provider, Model.provider_id == Provider.id)
            .where(Model.enabled == True)
        )).all()
        mlist = [m for m, _ in models]
        prov_by_pid = {}
        for m, p in models:
            prov_by_pid[m.provider_id] = p
        cooling = {}
        hc = HealthChecker()
        for m in mlist:
            if hc.is_cooling(m.id):
                cooling[m.id] = datetime.utcnow()
        scores = await rs.rank_all(db, mlist, prov_by_pid, cooling)
        if scores:
            top = scores[0]
            # 排除不可用的模型（skip excluded/unhealthy）
            for s in scores:
                if not s.excluded_reason:
                    return {
                        "model": s.model_id_str,
                        "provider": s.provider_name,
                        "display_name": s.display_name,
                        "final_score": s.final_score,
                        "rank": 1,
                    }
            # 全部不可用时返回第一个
            return {
                "model": top.model_id_str,
                "provider": top.provider_name,
                "display_name": top.display_name,
                "final_score": top.final_score,
                "excluded_reason": top.excluded_reason,
            }
    except Exception as e:
        print(f"[WARN] current-model ranking failed: {e}")
    # fallback: 用最近一次 auto 请求
    last = (await db.execute(
        select(RequestLog).where(RequestLog.requested_model == "auto")
        .order_by(desc(RequestLog.created_at)).limit(1)
    )).scalar_one_or_none()
    if not last:
        return {"model": None, "provider": None, "time": None}
    return {
        "model": last.routed_model,
        "provider": last.routed_provider,
        "time": last.created_at.isoformat() if last.created_at else None,
    }
# ========== 健康探测配置 ==========
class HealthConfigUpdate(BaseModel):
    interval_minutes: Optional[int] = None
@router.get("/health-config")
async def get_health_config():
    return {
        "interval_minutes": config.health_check.interval_minutes,
        "healthy_latency_threshold_ms": config.health_check.healthy_latency_threshold_ms,
        "ping_timeout_seconds": config.health_check.ping_timeout_seconds,
    }
@router.put("/health-config")
async def update_health_config(data: HealthConfigUpdate):
    from server.main import get_health_checker
    hc = get_health_checker()
    if data.interval_minutes is not None and 1 <= data.interval_minutes <= 1440:
        config.health_check.interval_minutes = data.interval_minutes
        save_config()
        # 重启调度器以应用新间隔
        if hc:
            hc.stop_scheduler()
            from server.db import AsyncSessionLocal
            from server.core.key_manager import KeyManager
            from server.core.crypto_service import get_crypto_service
            km = KeyManager(get_crypto_service())
            await hc.start_scheduler(AsyncSessionLocal, km)
    return {"ok": True, "interval_minutes": config.health_check.interval_minutes}
@router.post("/playground")
async def playground_chat(data: PlaygroundRequest, raw_request: Request, db: AsyncSession = Depends(get_db)):
    """Playground 测试聊天（含请求日志写入）"""
    import time, uuid, json as _json_mod
    from server.schemas.chat import ChatCompletionRequest, ChatMessage
    from server.api.v1_router import get_auto_router, _auto_route_with_runtime_fallback, _format_sse_chunk, _auto_request_with_cascade_fallback
    from server.models.request_log import RequestLog as _RL
    ar = get_auto_router()
    conversation_id = str(uuid.uuid4())
    _send_time = time.time()
    messages = [ChatMessage(**m) for m in data.messages]
    request = ChatCompletionRequest(
        model=data.model or "auto",
        messages=messages,
        stream=data.stream or False
    )
    is_auto = request.is_auto
    made_by_cascade = False
    route_result = None
    if not is_auto:
        if "/" in request.model:
            provider_name, model_id = request.model.split("/", 1)
            model = await _model_catalog.get_by_full_id(db, provider_name, model_id)
        else:
            models = await _model_catalog.list_models(db, enabled_only=True)
            model = next((m for m in models if m.model_id == request.model), None)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
        provider = await db.get(Provider, model.provider_id)
        result = await db.execute(
            select(ApiKey)
            .where(ApiKey.provider_id == provider.id, ApiKey.is_active == True)
            .limit(1)
        )
        key = result.scalar_one_or_none()
        if not key:
            raise HTTPException(status_code=400, detail="No active API key")
        api_key = _key_manager._crypto.decrypt(key.key_encrypted)
        from server.core.model_catalog import create_adapter_for_provider
        from server.core.auto_router import RouteResult
        adapter = create_adapter_for_provider(provider.api_type)
        route_result = RouteResult(
            success=True, model=model, provider=provider,
            api_key=api_key, adapter=adapter, fallback_count=0
        )
    elif data.stream:
        route_result, _ = await _auto_route_with_runtime_fallback(ar, db, request, conversation_id)
        if not route_result.success:
            # 写失败日志
            try:
                from server.db import AsyncSessionLocal as _LogSession
                async with _LogSession() as _ldb:
                    _ldb.add(_RL(
                        conversation_id=conversation_id, requested_model=request.model,
                        status="error", error_type="playground_routing_error",
                        error_msg=str(route_result.error)[:500],
                        user_ip=raw_request.client.host if raw_request.client else None,
                        request_body=_json_mod.dumps(request.model_dump(), ensure_ascii=False),
                    ))
                    await _ldb.commit()
            except Exception: pass
            return JSONResponse(status_code=503, content={"error": route_result.error})
    else:
        route_result, response, _attempt_errors = await _auto_request_with_cascade_fallback(ar, db, request, conversation_id)
        if not route_result.success:
            return JSONResponse(status_code=503, content={"error": response.get("error", "all_candidates_failed")})
        made_by_cascade = True

    model_id_full = f"{route_result.provider.name}/{route_result.model.model_id}"
    upstream_request = request.model_copy(update={"model": route_result.model.model_id})
    extra_headers = route_result.provider.headers if route_result.provider.headers else None

    # ─── 日志写入辅助 ───
    async def _write_log(status, resp_dict, latency_ms, error_msg=None):
        try:
            from server.db import AsyncSessionLocal as _LogSession
            usage = resp_dict.get("usage", {}) if resp_dict else {}
            pt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            ct = usage.get("completion_tokens") or usage.get("output_tokens") or 0
            req_s = _json_mod.dumps(request.model_dump(), ensure_ascii=False)
            resp_s = _json_mod.dumps(resp_dict, ensure_ascii=False)[:100000] if resp_dict else None
            async with _LogSession() as _ldb:
                _ldb.add(_RL(
                    conversation_id=conversation_id, requested_model=request.model,
                    routed_provider=route_result.provider.name if (route_result and route_result.success) else None,
                    routed_model=route_result.model.model_id if (route_result and route_result.success) else None,
                    status=status, latency_ms=latency_ms,
                    prompt_tokens=int(pt) if pt else 0,
                    completion_tokens=int(ct) if ct else 0,
                    fallback_count=route_result.fallback_count if route_result else 0,
                    user_ip=raw_request.client.host if raw_request.client else None,
                    error_type="upstream_error" if error_msg else None,
                    error_msg=(error_msg or "")[:500],
                    request_body=req_s, response_body=resp_s,
                ))
                await _ldb.commit()
        except Exception:
            pass

    if data.stream:
        async def wrap_stream():
            resp_dict = None
            try:
                async for chunk in route_result.adapter.stream_chat_completion(
                    upstream_request, route_result.api_key, route_result.provider.base_url, extra_headers
                ):
                    yield _format_sse_chunk(chunk, model_id_full)
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as e:
                yield _format_sse_chunk({"error": f"upstream_stream_failed: {type(e).__name__}: {str(e)[:200]}"}, model_id_full)
                await _write_log("error", resp_dict, int((time.time() - _send_time)*1000), str(e)[:500])
            yield b"data: [DONE]\n\n"
        return StreamingResponse(wrap_stream(), media_type="text/event-stream")

    if made_by_cascade:
        # response 已经在级联回退中获得
        result = response
    else:
        try:
            result = await route_result.adapter.chat_completion(
                upstream_request, route_result.api_key, route_result.provider.base_url, extra_headers
            )
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as e:
            await _write_log("error", None, int((time.time() - _send_time)*1000), str(e)[:500])
            return JSONResponse(status_code=503, content={"error": f"upstream_call_failed: {type(e).__name__}: {str(e)[:200]}"})

    if isinstance(result, dict) and "model" in result:
        result["model"] = model_id_full

    # 写成功日志
    resp_dict = result if isinstance(result, dict) else None
    await _write_log("success", resp_dict, int((time.time() - _send_time)*1000))
    return result
@router.get("/auto/ranking")
async def get_auto_ranking(db: AsyncSession = Depends(get_db)):
    """获取 Auto 综合评分排名，兼容旧前端入口。"""
    from server.core.ranking_service import RankingService
    rs = RankingService()
    models = (await db.execute(
        select(Model, Provider).join(Provider, Model.provider_id == Provider.id)
    )).all()
    mlist = [m for m, _ in models]
    prov_by_pid = {m.provider_id: p for m, p in models}
    scores = await rs.rank_all(db, mlist, prov_by_pid, {})
    weights = await rs.get_weights(db)
    ranking = []
    for i, s in enumerate(scores):
        ranking.append({
            "rank": i + 1,
            "model_id": s.model_id,
            "model_full_id": f"{s.provider_name}/{s.model_id_str}",
            "display_name": s.display_name,
            "provider_name": s.provider_name,
            "is_free": s.is_free,
            "speed_score": s.speed_score,
            "intel_score": s.intel_score,
            "stab_score": s.stab_score,
            "p50_ms": s.p50_ms,
            "success_rate": s.success_rate,
            "final_score": s.final_score,
            "excluded_reason": s.excluded_reason,
            "priority_boost": s.priority_boost,
            "auto_excluded": s.auto_excluded,
        })
    return {
        "weights": weights,
        "ranking": ranking,
        "best": next((x for x in ranking if not x.get("excluded_reason")), ranking[0] if ranking else None),
        "total_candidates": len(ranking),
        "timestamp": utc_iso(datetime.utcnow()),
    }