"""
配额追踪 + 代理池 + 媒体生成相关 admin 端点
- GET/PUT /admin/api/quota          配额追踪（今日/历史/provider 分布）
- GET/PUT /admin/api/proxy-pool      HTTP 代理池管理
- POST /admin/api/media/image         图片生成
"""
from typing import Optional, List
import json
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from server.db import AsyncSessionLocal
from server.config import get_config, save_config
from server.core.proxy_pool import get_proxy_pool, CURRENT_PROXY_URL
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

def _infer_image_mime(b64: str) -> str:
    """从 base64 头推断图片 MIME（OpenAI 等接口返回裸 base64，无 mime 字段）"""
    if b64.startswith("iVBORw0KGgo"):
        return "image/png"
    if b64.startswith("/9j/"):
        return "image/jpeg"
    if b64.startswith("UklGR"):
        return "image/webp"
    if b64.startswith("R0lGOD"):
        return "image/gif"
    return "image/png"


def _build_image_payload(result) -> str:
    """把图片结果结构化，便于分析页直接渲染图廊（url 或 base64）"""
    imgs = []
    for im in (result.images or []):
        if im.get("url"):
            imgs.append({"url": im["url"], "format": "url",
                         "revised_prompt": im.get("revised_prompt")})
        elif im.get("data"):
            data = im["data"]
            if len(data) > 3_000_000:
                data = data[:3_000_000]   # 超大图截断，避免单行日志过大
            imgs.append({"data": data, "format": "base64", "mime": _infer_image_mime(data),
                         "revised_prompt": im.get("revised_prompt")})
    return json.dumps({"type": "image_generation", "model": result.model,
                       "count": len(imgs), "images": imgs}, ensure_ascii=False)


def _build_video_payload(result) -> str:
    """把视频结果结构化，便于分析页直接渲染播放器"""
    vids = [{"url": v.get("url"), "duration": v.get("duration")} for v in (result.videos or [])]
    return json.dumps({"type": "video_generation", "model": result.model,
                       "count": len(vids), "videos": vids}, ensure_ascii=False)


async def _write_media_log(
    db, media_type: str, status: str, provider_name: str, model: str,
    prompt: str, latency_ms: float, error_msg: str = "",
    result_summary: str = "",
):
    """写一条媒体生成日志到 RequestLog 表"""
    import json as _json
    from server.models.request_log import RequestLog
    from server.core.request_logger import write_log  # v3.6 消息级去重写入
    rb = result_summary if result_summary else None
    # 媒体结果（图片 base64 / 视频 url）可能较大，放宽上限；极端情况下仅保留摘要
    if rb and len(rb) > 8_000_000:
        rb = _json.dumps({"type": media_type + "_generation", "model": model,
                          "count": 0, "note": "内容过大已省略存储"}, ensure_ascii=False)
    # 如实记录本次线请求是否走了代理：媒体适配器统一经代理池 request_with_fallback，
    # 代理池在发请求前把实际使用的代理 URL 写入 ContextVar，这里读取即可，
    # 避免日志里所有媒体请求都误显示「直连」（之前的 bug）
    _pu = CURRENT_PROXY_URL.get()
    _used_proxy = bool(_pu)
    async with AsyncSessionLocal() as log_db:
        await write_log(log_db,
            media_type=media_type,
            status=status,
            routed_provider=provider_name,
            routed_model=model,
            requested_model=model,
            latency_ms=int(latency_ms) if latency_ms else 0,
            error_msg=error_msg[:2000] if error_msg else None,
            request_body=_json.dumps({"prompt": prompt[:500]}, ensure_ascii=False),
            response_body=rb,
            used_proxy=_used_proxy,
            proxy_url=_pu if _used_proxy else None,
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
        summary = _build_image_payload(result)
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
        summary = _build_video_payload(result)
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
