"""
配额追踪 + 代理池 + 媒体生成相关 admin 端点
- GET/PUT /admin/api/quota          配额追踪（今日/历史/provider 分布）
- GET/PUT /admin/api/proxy-pool      HTTP 代理池管理
- POST /admin/api/media/image         图片生成
"""
from typing import Optional, List
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from server.db import AsyncSessionLocal
from server.config import get_config, save_config
from server.core.proxy_pool import get_proxy_pool
from server.models.provider import Provider
from server.models.api_key import ApiKey
from server.models.model import Model
from server.core.crypto_service import get_crypto_service
from server.core.key_rotator import get_key_rotator
from sqlalchemy import select

router = APIRouter(prefix="/admin/api")
config = get_config()
logger = logging.getLogger(__name__)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── 配额追踪已合并到分析页（/admin/api/analytics/*），不再保留独立端点 ──


# ── HTTP 代理池 ──────────────────────────────────────────

@router.get("/proxy-pool")
async def proxy_pool_status():
    pool = get_proxy_pool()
    snap = pool.status_snapshot()
    # 附带未脱敏的原始列表，供前端编辑/删除使用（避免回写脱敏后的密码）
    snap["raw_proxies"] = [
        {
            "name": p.get("name") or p.get("url", ""),
            "url": p.get("url", ""),
            "weight": int(p.get("weight", 1)),
        }
        for p in (config.proxy_pool.proxies or [])
    ]
    return snap


class ProxyConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    strategy: Optional[str] = None
    proxies: Optional[List[dict]] = None


@router.put("/proxy-pool")
async def update_proxy_pool(data: ProxyConfigUpdate):
    """更新代理池配置并热重载"""
    if data.enabled is not None:
        config.proxy_pool.enabled = bool(data.enabled)
    if data.strategy in ("round_robin", "weighted", "random"):
        config.proxy_pool.strategy = data.strategy
    if data.proxies is not None:
        config.proxy_pool.proxies = data.proxies
    save_config()
    # 热重载
    from server.core.proxy_pool import init_proxy_pool
    init_proxy_pool(config.proxy_pool.model_dump())
    return {"ok": True, "status": get_proxy_pool().status_snapshot()}


# ── 密钥轮换运行时状态 ──────────────────────────────────

@router.get("/keys/rotator-status")
async def key_rotator_status():
    return get_key_rotator().status_snapshot()


# ── 媒体生成：图片 ──────────────────────────────────────

class ImageGenPayload(BaseModel):
    provider_id: int
    model: Optional[str] = None
    prompt: str
    n: int = 1
    size: str = "1024x1024"
    quality: str = "standard"
    response_format: str = "b64_json"
    style: Optional[str] = None
    seed: Optional[int] = None
    negative_prompt: Optional[str] = None
    image_url: Optional[str] = None
    extra_params: Optional[dict] = None


# ── 媒体生成日志写入 ──────────────────────────────────────

async def _write_media_log(
    db, media_type: str, status: str, provider_name: str, model: str,
    prompt: str, latency_ms: float, error_msg: str = "",
    result_summary: str = "",
):
    """写一条媒体生成日志到 RequestLog 表"""
    import json as _json
    from server.models.request_log import RequestLog
    from server.core.request_logger import write_log  # v3.6 消息级去重写入
    async with AsyncSessionLocal() as log_db:
        await write_log(log_db,
            media_type=media_type,
            status=status,
            routed_provider=provider_name,
            routed_model=model,
            requested_model=model,
            latency_ms=int(latency_ms) if latency_ms else 0,
            error_msg=error_msg[:500] if error_msg else None,
            request_body=_json.dumps({"prompt": prompt[:500]}, ensure_ascii=False),
            response_body=result_summary[:2000] if result_summary else None,
            fallback_count=0,
            prompt_tokens=0,
            completion_tokens=0,
        )


@router.post("/media/image")
async def media_generate_image(data: ImageGenPayload, db: AsyncSession = Depends(get_db)):
    """图片生成统一入口 — 调用指定 provider 的 ImageAdapter"""
    from server.adapters.image_adapter import ImageAdapter, ImageGenRequest
    provider = await db.get(Provider, data.provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    # v3.5：模型级密钥选择（按 data.model 解析归属 key；解析不到则回退 provider 轮询）
    rotator = get_key_rotator()
    mdl_obj = None
    if data.model:
        mres = await db.execute(select(Model).where(Model.provider_id == provider.id, Model.model_id == data.model).limit(1))
        mdl_obj = mres.scalar_one_or_none()
    picked = await rotator.pick_key_for_model(db, mdl_obj) if mdl_obj else await rotator.pick_active_key(db, provider.id)
    if not picked:
        raise HTTPException(status_code=503, detail=f"No active key for {provider.name}")
    _key_id, api_key_plain = picked
    req = ImageGenRequest(
        prompt=data.prompt,
        model=data.model or "dall-e-3",
        n=max(1, min(data.n, 10)),
        size=data.size,
        quality=data.quality,
        response_format=data.response_format,
        style=data.style,
        seed=data.seed,
        negative_prompt=data.negative_prompt,
        image_url=data.image_url,
        extra_params=data.extra_params,
    )
    adapter = ImageAdapter()
    headers = provider.headers or None
    result = await adapter.generate_images(req, api_key_plain, provider.base_url, headers)
    # 写日志
    if result.success:
        rotator.mark_success(_key_id)
        summary = f"{len(result.images)} image(s) generated"
        await _write_media_log(db, "image", "success", provider.name, result.model,
                              data.prompt, result.elapsed_ms, result_summary=summary)
        return {
            "success": True,
            "model": result.model,
            "images": result.images,
            "elapsed_ms": round(result.elapsed_ms, 1),
        }
    else:
        rotator.mark_failure(_key_id)
        await _write_media_log(db, "image", "error", provider.name, req.model,
                              data.prompt, result.elapsed_ms, error_msg=result.error)
        return JSONResponse(status_code=502, content={"error": result.error})


# ── 媒体生成：视频 ──────────────────────────────────────

class VideoGenPayload(BaseModel):
    provider_id: int
    model: Optional[str] = None
    prompt: str
    n: int = 1
    duration: Optional[int] = None
    size: Optional[str] = None
    fps: Optional[int] = None
    image_url: Optional[str] = None       # 图生视频
    negative_prompt: Optional[str] = None  # 负面提示词
    seed: Optional[int] = None            # 固定随机种子
    extra_params: Optional[dict] = None


@router.post("/media/video")
async def media_generate_video(data: VideoGenPayload, db: AsyncSession = Depends(get_db)):
    """视频生成统一入口"""
    from server.adapters.video_adapter import VideoAdapter, VideoGenRequest
    provider = await db.get(Provider, data.provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    # v3.5：模型级密钥选择（按 data.model 解析归属 key；解析不到则回退 provider 轮询）
    rotator = get_key_rotator()
    mdl_obj = None
    if data.model:
        mres = await db.execute(select(Model).where(Model.provider_id == provider.id, Model.model_id == data.model).limit(1))
        mdl_obj = mres.scalar_one_or_none()
    picked = await rotator.pick_key_for_model(db, mdl_obj) if mdl_obj else await rotator.pick_active_key(db, provider.id)
    if not picked:
        raise HTTPException(status_code=503, detail=f"No active key for {provider.name}")
    _key_id, api_key_plain = picked
    req = VideoGenRequest(
        prompt=data.prompt,
        model=data.model or "",
        n=max(1, min(data.n, 4)),
        duration=data.duration,
        size=data.size,
        fps=data.fps,
        image_url=data.image_url,
        negative_prompt=data.negative_prompt,
        seed=data.seed,
        extra_params=data.extra_params,
    )
    adapter = VideoAdapter()
    headers = provider.headers or None
    result = await adapter.generate_videos(req, api_key_plain, provider.base_url, headers)
    # 写日志
    if result.success:
        rotator.mark_success(_key_id)
        summary = f"{len(result.videos)} video(s): " + ", ".join(v.get("url", "")[:100] for v in result.videos)
        await _write_media_log(db, "video", "success", provider.name, result.model,
                              data.prompt, result.elapsed_ms, result_summary=summary)
        return {
            "success": True,
            "model": result.model,
            "videos": result.videos,
            "elapsed_ms": round(result.elapsed_ms, 1),
        }
    else:
        rotator.mark_failure(_key_id)
        await _write_media_log(db, "video", "error", provider.name, req.model,
                              data.prompt, result.elapsed_ms, error_msg=result.error)
        return JSONResponse(status_code=502, content={"error": result.error})

@router.get("/headroom")
async def headroom_status(db: AsyncSession = Depends(get_db)):
    """Headroom 保留额度状态"""
    from server.core.headroom_manager import get_headroom_status
    return await get_headroom_status(db)


class HeadroomEntry(BaseModel):
    provider_id: int
    daily_token_limit: int
    label: str = ""


@router.put("/headroom")
async def update_headroom(entries: List[HeadroomEntry]):
    """更新 headroom 配置（完整替换）"""
    config.headroom.entries = [
        {"provider_id": e.provider_id,
         "daily_token_limit": e.daily_token_limit,
         "label": e.label}
        for e in entries
    ]
    config.headroom.enabled = bool(entries)
    save_config()
    return {"ok": True, "enabled": config.headroom.enabled, "entries": config.headroom.entries}


# ── Caveman / Ponytail 高级 saver 配置 ──────────────────────────

@router.get("/token-saver-extra")
async def get_saver_extra():
    return {
        "caveman_enabled": config.token_saver_extra.caveman_enabled,
        "ponytail_enabled": config.token_saver_extra.ponytail_enabled,
    }


class SaverExtraUpdate(BaseModel):
    caveman_enabled: Optional[bool] = None
    ponytail_enabled: Optional[bool] = None


@router.put("/token-saver-extra")
async def update_saver_extra(data: SaverExtraUpdate):
    if data.caveman_enabled is not None:
        config.token_saver_extra.caveman_enabled = bool(data.caveman_enabled)
    if data.ponytail_enabled is not None:
        config.token_saver_extra.ponytail_enabled = bool(data.ponytail_enabled)
    save_config()
    return {"ok": True, "config": config.token_saver_extra.model_dump()}
