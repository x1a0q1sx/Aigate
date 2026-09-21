"""u1s1 客户端证明（x-u1s1-attestation）。

平台「客户端完整性审查」的第二段握手：官方 CLI 在 GET /v1/models（带 DPoP proof）后，
从响应体 `client_attestation: {token, expires_in}` 取 token 缓存进内存，之后每个
**推理**请求附加 `x-u1s1-attestation: <token>`。只配 DPoP 不配 attestation 时：
/v1/models 能 200，chat/completions 仍 403「检测到请求来自非 u1s1 客户端」。

参数对齐 u1s1-cli@1.11.2 dist/device-auth.js：token 有效期 7 天（expires_in=604800），
距到期 24h 内视为临期（先交旧 token、后台刷新）；拉取失败冷却 30s；无 token 时请求最多
阻塞 4s 等首拉。
"""
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

REFRESH_MARGIN_S = 24 * 3600
COOLDOWN_S = 30.0
BLOCK_TIMEOUT_S = 4.0

# 进程级缓存（uvicorn 单进程部署；多 worker 时各自持 token，互不冲突）
_token: Optional[str] = None
_expires_at: float = 0.0
_last_failure: float = 0.0
_refreshing: Optional[asyncio.Task] = None


async def _do_fetch(base_url: str) -> None:
    """拉取并更新缓存；任何失败只记冷却，不向上抛。"""
    global _token, _expires_at, _last_failure
    try:
        import httpx
        from server.db import AsyncSessionLocal
        from server.core.oauth_client import get_oauth_client
        from server.core.dpop import sign_proof
        from server.core.provider_quirks import quirks_for

        async with AsyncSessionLocal() as db:
            material = await get_oauth_client().get_device_signing_material("u1s1", db)
        if not material:
            _last_failure = time.time()
            logger.warning("u1s1 attestation: 无设备签名材料（需重新登录）")
            return
        base = (base_url or "https://api.u1s1.io/v1").rstrip("/")
        url = base + "/models"
        q = quirks_for(base)
        headers = {"Accept": "application/json", **dict((q.default_headers if q else None) or {})}
        headers.update(sign_proof(material["priv"], material["bearer"], "GET", url))
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(url, headers=headers)
        body = r.json() if r.status_code == 200 and r.content else {}
        ca = (body or {}).get("client_attestation") or {}
        tok = ca.get("token")
        if not (isinstance(tok, str) and tok):
            _last_failure = time.time()
            logger.warning("u1s1 attestation: 拉取未获得 token（http %s）", r.status_code)
            return
        _token = tok
        _expires_at = time.time() + float(ca.get("expires_in") or 86400)
        logger.info("u1s1 attestation 已刷新（有效约 %.1f 天）",
                    (_expires_at - time.time()) / 86400)
    except Exception as e:
        _last_failure = time.time()
        logger.warning("u1s1 attestation fetch error: %s", e)


def _fetch_task(base_url: str) -> asyncio.Task:
    """单飞：复用未完成的首拉任务，避免并发重复打 /v1/models。"""
    global _refreshing
    if _refreshing is None or _refreshing.done():
        _refreshing = asyncio.create_task(_do_fetch(base_url))
    return _refreshing


async def get_attestation(base_url: str = "") -> Optional[str]:
    """返回当前可用的 attestation token；无/失败返回 None。"""
    global _token
    now = time.time()
    if _token and now >= _expires_at:
        _token = None
        now = time.time()
    if _token:
        if now < _expires_at - REFRESH_MARGIN_S:
            return _token
        # 临期：旧 token 仍可用，后台刷新不阻塞本次请求
        _fetch_task(base_url)
        return _token
    if now - _last_failure < COOLDOWN_S:
        return None
    try:
        # shield：首拉最多等 4s，超时不能取消掉共享任务
        await asyncio.wait_for(asyncio.shield(_fetch_task(base_url)),
                               timeout=BLOCK_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        pass
    now = time.time()
    return _token if (_token and now < _expires_at) else None
