"""
管理面板认证模块
- 用户名/密码登录（bcrypt 校验）
- session token（内存存储，重启失效）
- FastAPI 中间件：/admin/* 需要 session，/v1/* 保持开放（用 aigate_api_key）
"""
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

# 内存 session 存储: token -> {username, expires_at}
_sessions: Dict[str, dict] = {}

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
_PUBLIC_PATHS = {"/", "/admin/api/auth/login", "/admin/api/auth/check"}


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "username": username,
        "expires_at": datetime.utcnow() + timedelta(hours=config.auth.session_timeout_hours),
    }
    return token


def validate_session(token: str) -> bool:
    sess = _sessions.get(token)
    if not sess:
        return False
    if datetime.utcnow() > sess["expires_at"]:
        _sessions.pop(token, None)
        return False
    return True


def destroy_session(token: str):
    _sessions.pop(token, None)


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
            if token and validate_session(token):
                return await call_next(request)
            # 未认证：API 返回 401 JSON，页面返回 401 让前端跳转登录
            if path.startswith("/admin/api/"):
                return JSONResponse(status_code=401, content={"detail": "未登录或 session 已过期"})
            # SPA 页面：返回 index.html（前端 router 会拦截跳登录页）
            from fastapi.responses import FileResponse
            client_dist = __import__("pathlib").Path(__file__).resolve().parent.parent.parent / "client" / "dist"
            if client_dist.exists():
                return FileResponse(str(client_dist / "index.html"))
            return JSONResponse(status_code=401, content={"detail": "未登录"})

        # 其他路径放行
        return await call_next(request)
