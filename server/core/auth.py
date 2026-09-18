"""
管理面板认证模块
- 用户名/密码登录（bcrypt 校验）
- session token（DB 持久化 + 进程内 L1 缓存：服务重启不掉线；
  过期时长 config.auth.session_timeout_hours，活跃使用滑动续期）
- FastAPI 中间件：/admin/* 需要 session，/v1/* 保持开放（用 aigate_api_key）
"""
import logging
import time
import secrets
import bcrypt
from datetime import datetime, timedelta
from typing import Optional, Dict
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from server.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

# L1 缓存: token -> {username, expires_at}（权威数据在 admin_sessions 表）
_sessions: Dict[str, dict] = {}


def _db_session():
    """独立的 DB session 工厂（测试可 monkeypatch；延迟导入避免循环依赖）"""
    from server.db import AsyncSessionLocal
    return AsyncSessionLocal()

# 不需要认证的路径前缀
_PUBLIC_PREFIXES = (
    "/v1/",           # OpenAI 兼容 API（用自己的 aigate_api_key 鉴权）
    "/v1/messages",   # Anthropic 兼容
    "/admin/api/auth/login",
    "/admin/api/auth/check",
    "/assets/",
    "/vite.svg",
)

# 不需要认证的精确路径
# OAuth 回调是 provider 在用户浏览器中顶层导航触发的 GET，不可能携带
# Authorization 头（会话只存 localStorage），必须精确豁免；
# 该端点自身的安全防线是 state/PKCE 校验，而非登录态。
_PUBLIC_PATHS = {"/", "/admin/api/auth/login", "/admin/api/auth/check", "/admin/oauth/callback"}


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def _session_ttl() -> timedelta:
    return timedelta(hours=config.auth.session_timeout_hours)


async def create_session(username: str) -> str:
    """创建会话：落库（重启不丢）+ L1 缓存；DB 异常时退化为纯内存会话。"""
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + _session_ttl()
    _sessions[token] = {"username": username, "expires_at": expires}
    try:
        from sqlalchemy import delete as _sa_delete
        from server.models.admin_session import AdminSession
        async with _db_session() as db:
            # 顺手清理过期会话，表不会无限膨胀
            await db.execute(_sa_delete(AdminSession).where(
                AdminSession.expires_at < datetime.utcnow()))
            db.add(AdminSession(token=token, username=username, expires_at=expires))
            await db.commit()
    except Exception as e:
        logger.warning("session 落库失败（退化为内存会话，重启后需重登）: %s", e)
    return token


async def validate_session(token: str) -> bool:
    """校验会话；L1 未命中回源 DB，活跃会话剩余不足一半 TTL 时滑动续期。"""
    now = datetime.utcnow()
    sess = _sessions.get(token)
    if sess is None:
        try:
            from server.models.admin_session import AdminSession
            async with _db_session() as db:
                row = await db.get(AdminSession, token)
                if row is None:
                    return False
                if row.expires_at <= now:
                    await db.delete(row)
                    await db.commit()
                    return False
                sess = {"username": row.username, "expires_at": row.expires_at}
                _sessions[token] = sess
        except Exception as e:
            logger.warning("session 查库失败: %s", e)
            return False
    if now > sess["expires_at"]:
        _sessions.pop(token, None)
        try:
            from sqlalchemy import delete as _sa_delete
            from server.models.admin_session import AdminSession
            async with _db_session() as db:
                await db.execute(_sa_delete(AdminSession).where(AdminSession.token == token))
                await db.commit()
        except Exception:
            pass
        return False
    # 滑动续期：持续使用中不会中途过期；写库频率 ≤ 每 TTL/2 一次
    if sess["expires_at"] - now < _session_ttl() / 2:
        sess["expires_at"] = now + _session_ttl()
        try:
            from server.models.admin_session import AdminSession
            async with _db_session() as db:
                row = await db.get(AdminSession, token)
                if row is not None:
                    row.expires_at = sess["expires_at"]
                    await db.commit()
        except Exception as e:
            logger.warning("session 续期写库失败: %s", e)
    return True


async def destroy_session(token: str):
    _sessions.pop(token, None)
    try:
        from sqlalchemy import delete as _sa_delete
        from server.models.admin_session import AdminSession
        async with _db_session() as db:
            await db.execute(_sa_delete(AdminSession).where(AdminSession.token == token))
            await db.commit()
    except Exception as e:
        logger.warning("session 删库失败: %s", e)


async def clear_all_sessions():
    """清空全部会话（改密码等场景强制全员重新登录）。"""
    _sessions.clear()
    try:
        from sqlalchemy import delete as _sa_delete
        from server.models.admin_session import AdminSession
        async with _db_session() as db:
            await db.execute(_sa_delete(AdminSession))
            await db.commit()
    except Exception as e:
        logger.warning("session 全量清库失败: %s", e)


def extract_token(request: Request) -> Optional[str]:
    # 1. Authorization header
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # 2. Cookie
    cookie = request.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("aigate_session="):
            return part[len("aigate_session="):]
    return None


def _is_admin_api_path(path: str) -> bool:
    """未认证时应返回 401 JSON 的管理端接口路径。

    除 /admin/api/* 外还有 /admin/oauth/*（OAuth 管理接口）。若按"SPA 页面"
    处理会返回 200+index.html，前端 res.ok 后 JSON.parse 直接炸。
    """
    return path.startswith("/admin/api/") or path.startswith("/admin/oauth/")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 认证关闭 → 全部放行
        if not config.auth.enabled:
            return await call_next(request)

        # 公开路径放行
        if path in _PUBLIC_PATHS:
            return await call_next(request)
        for prefix in _PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # /admin/* 和 /admin/api/* 需要认证
        if path.startswith("/admin"):
            token = extract_token(request)
            if token and await validate_session(token):
                return await call_next(request)
            # 未认证：API 返回 401 JSON，页面返回 401 让前端跳转登录
            if _is_admin_api_path(path):
                return JSONResponse(status_code=401, content={"detail": "未登录或 session 已过期"})
            # SPA 页面：返回 index.html（前端 router 会拦截跳登录页）
            from fastapi.responses import FileResponse
            client_dist = __import__("pathlib").Path(__file__).resolve().parent.parent.parent / "client" / "dist"
            if client_dist.exists():
                return FileResponse(str(client_dist / "index.html"))
            return JSONResponse(status_code=401, content={"detail": "未登录"})

        # 其他路径放行
        return await call_next(request)
