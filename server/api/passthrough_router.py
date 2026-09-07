"""A3: OpenAI 兼容轻接口透传（/v1/embeddings、/v1/images/generations）。

与 chat 共用：鉴权（主密钥/网关密钥）、别名解析、直连模型解析、
统一凭证解析器（标准密钥轮换/代理）。不走 combo/auto/embeddings 级联。
请求日志照常落库（media_type 区分 embedding/image），token 取上游 usage。
"""
import time
import urllib.parse
from typing import Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import AsyncSessionLocal
from server.models.model import Model
from server.models.provider import Provider
from server.api.v1_router import verify_aigate_api_key
from server.core.alias_service import resolve_alias
from server.core.credential_resolver import resolve_credential_async
from server.core.request_logger import write_log

router = APIRouter(prefix="/v1")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


def _proxy_kwargs(provider) -> dict:
    """per-provider 代理（沿用 proxy_pool 语义：proxy_enabled 才走）"""
    if getattr(provider, "proxy_enabled", False):
        from server.core.proxy_pool import get_proxy_pool
        return get_proxy_pool().proxied_kwargs(force=True)
    return {}


def _build_url(base_url: str, path: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return base + path
    return base + "/v1" + path


async def _resolve_direct(db: AsyncSession, name: str) -> Tuple[Optional[Provider], Optional[Model]]:
    """把请求名解析成 (provider, model)：别名 → provider/model → 全局唯一 model_id。"""
    if not name:
        return None, None
    aliased = await resolve_alias(name)
    name = aliased or name
    if "/" in name:
        prov_name, mod_id = name.split("/", 1)
        provider = (await db.execute(
            select(Provider).where(Provider.name == prov_name).limit(1)
        )).scalar_one_or_none()
        if not provider:
            return None, None
        model = (await db.execute(
            select(Model).where(
                Model.provider_id == provider.id,
                Model.model_id == mod_id,
                Model.enabled.is_(True),
            ).limit(1)
        )).scalar_one_or_none()
        return provider, model
    # 无前缀：全局唯一匹配
    rows = (await db.execute(
        select(Model, Provider)
        .join(Provider, Provider.id == Model.provider_id)
        .where(Model.model_id == name, Model.enabled.is_(True), Provider.enabled.is_(True))
    )).all()
    if len(rows) == 1:
        return rows[0][1], rows[0][0]
    return None, None


async def _passthrough(
    raw_request: Request,
    db: AsyncSession,
    path: str,
    media_type: str,
    model_field: str = "model",
):
    """共用透传逻辑：转发到解析出的 provider 并按结果落日志。"""
    await verify_aigate_api_key(raw_request)
    _t0 = time.time()
    try:
        body = await raw_request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "invalid_json"}})
    name = str(body.get(model_field) or "")
    if not name or name == "auto" or name.startswith("combo:"):
        return JSONResponse(status_code=400, content={
            "error": {"message": f"'{name}' 不支持 {path}（仅直连模型：provider/model 或别名）"}})
    provider, model = await _resolve_direct(db, name)
    if not provider or not model:
        return JSONResponse(status_code=404, content={
            "error": {"message": f"模型 '{name}' 不存在或未启用"}})
    rc = await resolve_credential_async(provider, model, db)
    if not rc.ok or not rc.base_url:
        return JSONResponse(status_code=502, content={
            "error": {"message": rc.error or "无可用凭证"}})
    headers = {"Content-Type": "application/json"}
    if rc.api_key:
        headers["Authorization"] = f"Bearer {rc.api_key}"
    if rc.extra_headers:
        headers.update(rc.extra_headers)
    upstream_body = {k: v for k, v in body.items()}
    upstream_body[model_field] = model.model_id
    try:
        async with httpx.AsyncClient(timeout=120, **_proxy_kwargs(provider)) as client:
            resp = await client.post(_build_url(rc.base_url, path),
                                     json=upstream_body, headers=headers)
    except Exception as e:
        await write_log(db, requested_model=name, routed_provider=provider.name,
                        routed_model=model.model_id, status="error",
                        media_type=media_type, error_type="upstream_error",
                        error_msg=str(e)[:500], latency_ms=int((time.time() - _t0) * 1000),
                        used_proxy=bool(_proxy_kwargs(provider)))
        return JSONResponse(status_code=502, content={"error": {"message": f"上游请求失败: {str(e)[:200]}"}})
    latency_ms = int((time.time() - _t0) * 1000)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    # 落日志（尽力提取 usage）
    usage = (payload or {}).get("usage") or {}
    await write_log(db,
                    requested_model=name, routed_provider=provider.name,
                    routed_model=model.model_id,
                    status="success" if resp.status_code == 200 else "error",
                    media_type=media_type, http_status=resp.status_code,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    error_type=None if resp.status_code == 200 else "upstream_error",
                    error_msg=None if resp.status_code == 200 else str(payload)[:500],
                    latency_ms=latency_ms, used_proxy=bool(_proxy_kwargs(provider)))
    return JSONResponse(status_code=resp.status_code, content=payload)


@router.post("/embeddings")
async def create_embeddings(raw_request: Request, db: AsyncSession = Depends(get_db)):
    """OpenAI 兼容 embeddings 透传（A3）"""
    return await _passthrough(raw_request, db, "/embeddings", "embedding")


@router.post("/images/generations")
async def create_images(raw_request: Request, db: AsyncSession = Depends(get_db)):
    """OpenAI 兼容图片生成透传（A3；媒体中心之外的通用入口）"""
    return await _passthrough(raw_request, db, "/images/generations", "image")
