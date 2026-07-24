"""
OAuth 管理 + 调度路由
- GET  /admin/oauth/providers        列出全部已注册 OAuth provider（静态元数据）
- GET  /admin/oauth/connections      列出已连接的 OAuth 帐号
- POST /admin/oauth/authorize/{code} 触发授权流程返回 authorize_url
- GET  /admin/oauth/callback         OAuth 回调入口（URI？code=...&state=...）
- POST /admin/oauth/refresh/{id}     手动强制刷新
- DELETE /admin/oauth/connections/{id}  断开某条 OAuth 连接（删除 token）

调度器：refresh_scheduler 后台周期性扫描所有 oauth_tokens
  - 对 expires_at 临近的 token 主动 refresh
  - 异常安全：失败仅记 last_error 不抛
"""
from typing import Optional
import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from server.db import AsyncSessionLocal
from server.config import get_config
from server.models.oauth_token import OAuthToken
from server.core.oauth_registry import get_all_oauth_providers, get_oauth_provider
from server.core.oauth_client import get_oauth_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/oauth")
config = get_config()


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── 静态元数据 ──────────────────────────────────────────

@router.get("/providers")
async def list_oauth_providers():
    """列出全部已注册 OAuth provider"""
    out = []
    for p in get_all_oauth_providers():
        out.append({
            "code": p.code,
            "name": p.name,
            "scope": p.scope,
            "use_pkce": p.use_pkce,
            "refresh_lead_seconds": p.refresh_lead_seconds,
            "api_base_url": p.api_base_url,
            "notes": p.notes,
            "extra_params": p.extra_params or {},
            "client_id": (p.client_id or "")[:16] + ("…" if p.client_id and len(p.client_id) > 16 else ""),
            "authorize_url": p.authorize_url,
        })
    return out


# ── 已连接帐号 ──────────────────────────────────────────

@router.get("/connections")
async def list_connections(db: AsyncSession = Depends(get_db)):
    return await get_oauth_client().list_connections(db)


# ── 触发授权 ──────────────────────────────────────────

@router.post("/authorize/{provider_code}")
async def start_oauth_authorize(provider_code: str, request: Request, owner: str = "__default"):
    """生成 authorize_url + state + PKCE verifier（device_poll 走另一组响应）"""
    provider = get_oauth_provider(provider_code)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider_code}")
    # ── device_poll 流程（如 CodeBuddy CN）────
    if (provider.extra_params or {}).get("auth_mode") == "device_poll":
        async with AsyncSessionLocal() as db:
            r = await get_oauth_client().start_device_poll(provider_code, db, owner=owner)
        if "error" in r:
            raise HTTPException(status_code=400, detail=r["error"])
        # 前端用 login_url 弹窗，后端已经在轮询
        return {
            "device_poll": True,
            "state": r["state"],
            "login_url": r["login_url"],
            "poll_interval_ms": r["poll_interval_ms"],
            "message": "请在新窗口完成登录，登录成功后系统会自动获取 token",
        }
    # 运行时 redirect_uri：用本机 incoming host:port 替换默认 localhost:8000
    redirect_override = None
    if request:
        # 优先用请求 host（即用户访问 AIGate 的地址，不要用 127.0.0.1）
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        scheme = request.headers.get("x-forwarded-proto") or "http"
        if host:
            redirect_override = f"{scheme}://{host}/admin/oauth/callback"
    client = get_oauth_client()
    client.redirect_override = redirect_override
    url, state, _verifier = client.build_authorize_url(provider, owner=owner)
    if not url and (provider.extra_params or {}).get("device_code_only"):
        return {"device_code": True, "message": "请使用 Qoder 设备授权流手动输入"}
    if not url:
        raise HTTPException(status_code=400, detail="this provider has no authorize_url")
    return {"authorize_url": url, "state": state, "provider": provider_code}


# ── 导入浏览器 / 桌面客户端 token（给 CodeBuddy CN / Kimchi / 任何已登录的应用用） ──

class ImportedTokenPayload(BaseModel):
    provider_code: str                       # codebuddy_cn / kimchi / 任意 OAuth provider code
    access_token: str
    refresh_token: str = ""
    expires_in: int = 3600
    scope: str = ""
    owner: str = "__default"


@router.post("/import-token")
async def import_oauth_token(data: ImportedTokenPayload, db: AsyncSession = Depends(get_db)):
    """
    用户从其它桌面客户端（如 CodeBuddy CLI、Kimchi 浏览器扩展）手动复制 token 后导入。
    这是因为部分 provider（CodeBuddy CN、Kimchi）使用了非标准 OAuth 流程，
    逆向工程代价高且不稳定 — 直接让用户粘贴他们已有的 token 最稳。
    """
    provider = get_oauth_provider(data.provider_code)
    if not provider:
        raise HTTPException(status_code=404, detail=f"unknown provider {data.provider_code}")
    if not data.access_token:
        raise HTTPException(status_code=400, detail="access_token is required")
    tok = {
        "access_token": data.access_token,
        "refresh_token": data.refresh_token,
        "expires_in": max(1, int(data.expires_in or 3600)),
        "token_type": "Bearer",
        "scope": data.scope or "",
    }
    client = get_oauth_client()
    saved = await client._save_token(db, data.provider_code, data.owner, tok)
    return {"ok": True, "id": saved.id, "provider_code": data.provider_code, "owner": data.owner}


# ── 回调 ──────────────────────────────────────────

@router.get("/callback")
async def oauth_callback(code: str, state: str, db: AsyncSession = Depends(get_db)):
    """OAuth 回调：用 code 换 token 并持久化"""
    if not code or not state:
        return JSONResponse(status_code=400, content={"error": "missing code or state"})
    ok, msg, token = await get_oauth_client().exchange_code_for_token(
        provider_code="", code=code, state=state, db=db
    )
    if not ok:
        return JSONResponse(status_code=400, content={"error": msg})
    # 重定向回前端的 OAuth 连接页
    return RedirectResponse(url="/oauth?auth=success", status_code=302)


# ── 手动刷新 ──────────────────────────────────────────

@router.post("/refresh/{connection_id}")
async def manual_refresh(connection_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(OAuthToken, connection_id)
    if not row:
        raise HTTPException(status_code=404, detail="connection not found")
    ok, msg = await get_oauth_client().refresh_token(row.provider_code, db, row.owner)
    return {"ok": ok, "message": msg}


# ── 断开连接（删除） ──────────────────────────────────────────

@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(OAuthToken, connection_id)
    if not row:
        raise HTTPException(status_code=404, detail="connection not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


# ── 调度器：主动刷新即将到期的 token ──────────────────────────────

async def _refresh_scheduler_loop():
    """每 60 秒扫一次即将到期的 token 主动刷新"""
    logger.info("OAuth refresh scheduler started (60s interval)")
    while True:
        try:
            await asyncio.sleep(60)
            now = __import__("datetime").datetime.utcnow()
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(OAuthToken).where(OAuthToken.is_active == True)
                )).scalars().all()
                client = get_oauth_client()
                from datetime import timedelta
                for row in rows:
                    provider = get_oauth_provider(row.provider_code)
                    if not provider:
                        continue
                    lead = provider.refresh_lead_seconds
                    if row.expires_at and now >= (row.expires_at - timedelta(seconds=lead)):
                        try:
                            ok, msg = await client.refresh_token(row.provider_code, db, row.owner)
                            if ok:
                                logger.info("OAuth token refreshed: %s/%s",
                                            row.provider_code, row.owner)
                            else:
                                logger.warning("OAuth refresh failed %s/%s: %s",
                                                row.provider_code, row.owner, msg)
                        except Exception as e:
                            logger.error("OAuth refresh error %s/%s: %s",
                                          row.provider_code, row.owner, e)
        except asyncio.CancelledError:
            logger.info("OAuth refresh scheduler cancelled")
            break
        except Exception as e:
            logger.error("refresh loop error: %s", e)
            await asyncio.sleep(30)  # 出错防卡死


_scheduler_task: Optional[asyncio.Task] = None


def start_oauth_refresh_scheduler():
    """启动后台刷新调度器（在 main.py lifespan 里调用）"""
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        try:
            _scheduler_task = asyncio.create_task(_refresh_scheduler_loop())
        except RuntimeError:
            # 没运行中的 event loop（启动期外）— 忽略
            pass


def stop_oauth_refresh_scheduler():
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
