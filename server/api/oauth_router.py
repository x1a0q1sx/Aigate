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
            "adapter_api_type": p.adapter_api_type,
            "static_models": p.static_models or [],
        })
    return out


# ── 已连接帐号 ──────────────────────────────────────────

@router.get("/connections")
async def list_connections(db: AsyncSession = Depends(get_db)):
    return await get_oauth_client().list_connections(db)


# ── 触发授权 ──────────────────────────────────────────

async def _next_owner_for_provider(db: AsyncSession, provider_code: str) -> str:
    """给「新增账号」分配一个不与现有连接冲突的账号名。

    约定：该服务商第一条连接叫 __default（主账号，路由默认取它）；
    之后新增依次 account-2 / account-3 …。已有 account-N 时跳过，绝不覆盖。"""
    rows = (await db.execute(
        select(OAuthToken.owner).where(OAuthToken.provider_code == provider_code)
    )).scalars().all()
    existing = {r for r in rows if r}
    if "__default" not in existing:
        return "__default"
    n = 2
    while f"account-{n}" in existing:
        n += 1
    return f"account-{n}"


@router.post("/authorize/{provider_code}")
async def start_oauth_authorize(provider_code: str, request: Request,
                                owner: Optional[str] = None, new_account: bool = False):
    """生成 authorize_url + state + PKCE verifier（device_poll 走另一组响应）

    owner 未显式指定时自动分配：首次连接 → __default；已有账号 → account-N。
    因此「一键连接」在已连接状态下是**新增账号**而不是静默覆盖主账号凭证
    （此前固定写 __default，第二次登录会覆盖第一个账号 —— 用户实测踩到）。"""
    provider = get_oauth_provider(provider_code)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider_code}")
    if not owner or not str(owner).strip():
        async with AsyncSessionLocal() as _db:
            owner = await _next_owner_for_provider(_db, provider_code)
    else:
        owner = str(owner).strip()
    # ── device_poll 流程（如 CodeBuddy CN / 国际服）────
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
    # ── u1s1 设备登录（浏览器批准 → 轮询收 api_key，免客户端）────
    if (provider.extra_params or {}).get("auth_mode") == "u1s1_device":
        async with AsyncSessionLocal() as db:
            r = await get_oauth_client().start_u1s1_device(provider_code, db, owner=owner)
        if "error" in r:
            raise HTTPException(status_code=400, detail=r["error"])
        return {
            "device_poll": True,
            "state": r["state"],
            "login_url": r["login_url"],
            "poll_interval_ms": r["poll_interval_ms"],
            "message": "请在新窗口登录 u1s1 并批准本设备，批准后系统会自动收取 api_key",
        }
    # ── Qoder 设备流（本地 PKCE+nonce → 轮询收 dt- token + 签名元数据）────
    if (provider.extra_params or {}).get("auth_mode") == "qoder_device":
        async with AsyncSessionLocal() as db:
            r = await get_oauth_client().start_qoder_device(provider_code, db, owner=owner)
        if "error" in r:
            raise HTTPException(status_code=400, detail=r["error"])
        return {
            "device_poll": True,
            "state": r["state"],
            "login_url": r["login_url"],
            "poll_interval_ms": r["poll_interval_ms"],
            "message": "请在新窗口登录 Qoder 并选择账号，批准后系统会自动收取 device token（约 30 天有效）",
        }
    # ── LobsterAI：portal 回调锁死 127.0.0.1 → 手动粘贴回调 URL 模式 ────
    if (provider.extra_params or {}).get("auth_mode") == "lobsterai":
        r = await get_oauth_client().start_lobsterai_login(owner=owner)
        return {
            "manual_callback": True,
            "state": r["state"],
            "login_url": r["login_url"],
            "callback_hint": r["callback_hint"],
            "message": ("请在新窗口完成 LobsterAI 登录。登录后浏览器会跳到一个"
                        "打不开的 127.0.0.1 地址（正常现象）—— 把地址栏完整 URL "
                        "粘贴回本页「完成登录」输入框即可。"),
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

class ManualCallbackPayload(BaseModel):
    provider_code: str
    callback_url: str = ""     # 用户粘贴的完整回调 URL（最省事的输入形态）
    code: str = ""
    state: str = ""


@router.post("/complete-callback")
async def complete_manual_callback(data: ManualCallbackPayload,
                                   db: AsyncSession = Depends(get_db)):
    """手动完成回调（portal 把回调锁死 127.0.0.1 的 provider 用）。

    当前用于 LobsterAI：登录后浏览器落到 `http://127.0.0.1:18090/...`
    打不开是正常的，用户把地址栏整段 URL 粘贴回来即可（也接受只填 code+state）。
    """
    provider = get_oauth_provider(data.provider_code)
    if not provider:
        raise HTTPException(status_code=404, detail=f"unknown provider {data.provider_code}")
    if (provider.extra_params or {}).get("auth_mode") != "lobsterai":
        raise HTTPException(status_code=400,
                            detail=f"{data.provider_code} 不支持手动回调模式")
    ok, msg, saved = await get_oauth_client().complete_lobsterai_login(
        data.code, data.state, db, callback_url=data.callback_url,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "id": saved.id if saved else None,
            "provider_code": data.provider_code, "owner": saved.owner if saved else None}


@router.get("/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = "", db: AsyncSession = Depends(get_db)):
    """OAuth 回调：用 code 换 token 并持久化，结果自动落到 /providers/oauth 页。

    state 可选：Cline 类 authorize 端点不保证回显 state，此时按
    start_oauth_authorize 时登记的挂起会话（provider+owner+redirect）收尾。
    用户在授权页拒绝时 provider 回跳 ?error=...（无 code），同样落到页面提示。"""
    from urllib.parse import quote

    def _landing(ok: bool, msg: str = ""):
        q = "?oauth=success" if ok else "?oauth=error&msg=" + quote(msg or "unknown error")
        return RedirectResponse(url="/providers/oauth" + q, status_code=302)

    if error:
        return _landing(False, f"对方拒绝了授权（{error}）")
    if not code:
        return JSONResponse(status_code=400, content={"error": "missing code"})
    client = get_oauth_client()
    try:
        if state and "|" in state:
            ok, msg, token = await client.exchange_code_for_token(
                provider_code="", code=code, state=state, db=db
            )
        else:
            ok, msg, token = await client.complete_pending(code, db)
    except Exception as e:
        ok, msg = False, f"{type(e).__name__}: {e}"
    if not ok:
        return _landing(False, msg)
    return _landing(True)


# ── 额度/余额查询（端口自 9router usage handlers） ─────────────

@router.get("/connections/{connection_id}/usage")
async def connection_usage(connection_id: int, force: bool = False,
                           db: AsyncSession = Depends(get_db)):
    """按连接查上游额度/余额。失败与未实现均以 message 字段表达（HTTP 恒 200）。"""
    from server.core.oauth_usage import get_connection_usage
    row = await db.get(OAuthToken, connection_id)
    if not row:
        raise HTTPException(status_code=404, detail="connection not found")
    from server.core.oauth_client import get_oauth_client
    token = get_oauth_client()._crypto.decrypt(row.access_token_enc)
    result = await get_connection_usage(row.provider_code, token, force=force)
    return {**result, "connection_id": row.id, "owner": row.owner}


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

class ConnectionUpdate(BaseModel):
    owner: Optional[str] = None      # 账号显示名（同服务商下唯一，用于区分多账号）
    is_active: Optional[bool] = None

@router.patch("/connections/{connection_id}")
async def update_connection(connection_id: int, payload: ConnectionUpdate,
                            db: AsyncSession = Depends(get_db)):
    """编辑 OAuth 连接：改账号名（owner）/ 启用停用。

    多账号场景下 owner 既是展示名也是路由寻址键（pick_access_token 按 owner 取 token），
    故同 provider_code 下不得重名——重名会让两条连接互相覆盖。"""
    row = await db.get(OAuthToken, connection_id)
    if not row:
        raise HTTPException(status_code=404, detail="connection not found")
    if payload.owner is not None:
        new_owner = (payload.owner or "").strip()
        if not new_owner:
            raise HTTPException(status_code=400, detail="账号名不能为空")
        if len(new_owner) > 100:
            raise HTTPException(status_code=400, detail="账号名过长（≤100 字符）")
        if new_owner != row.owner:
            dup = (await db.execute(
                select(OAuthToken).where(
                    OAuthToken.provider_code == row.provider_code,
                    OAuthToken.owner == new_owner,
                    OAuthToken.id != connection_id,
                ).limit(1)
            )).scalars().first()
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail=f"该服务商下已存在账号「{new_owner}」，请换一个名字")
            row.owner = new_owner
    if payload.is_active is not None:
        row.is_active = bool(payload.is_active)
    await db.commit()
    # 改名影响凭证拾取（owner 是寻址键），清掉 in-flight 锁避免旧键残留
    try:
        get_oauth_client()._flight_locks.pop(f"{row.provider_code}::{row.owner}", None)
    except Exception:
        pass
    return {"ok": True, "id": row.id, "owner": row.owner, "is_active": row.is_active}


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
