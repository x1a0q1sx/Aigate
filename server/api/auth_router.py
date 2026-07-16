"""
认证 API 端点
POST /admin/api/auth/login   → 用户名/密码登录，返回 session token
POST /admin/api/auth/logout  → 注销
GET  /admin/api/auth/check   → 检查当前 session 是否有效
PUT  /admin/api/auth/password → 修改密码
"""
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from server.core.auth import (
    verify_password, create_session, destroy_session, validate_session,
    extract_token, config,
)

router = APIRouter(prefix="/admin/api/auth", tags=["auth"])


class LoginPayload(BaseModel):
    username: str
    password: str


class PasswordChangePayload(BaseModel):
    old_password: str
    new_password: str


@router.post("/login")
async def login(payload: LoginPayload):
    if not config.auth.enabled:
        return {"ok": True, "token": "", "message": "auth disabled"}
    if payload.username != config.auth.username:
        return JSONResponse(status_code=401, content={"detail": "用户名或密码错误"})
    if not verify_password(payload.password, config.auth.password_hash):
        return JSONResponse(status_code=401, content={"detail": "用户名或密码错误"})
    token = create_session(payload.username)
    return {"ok": True, "token": token, "username": payload.username}


@router.post("/logout")
async def logout(request: Request):
    token = extract_token(request)
    if token:
        destroy_session(token)
    return {"ok": True}


@router.get("/check")
async def check_session(request: Request):
    if not config.auth.enabled:
        return {"authenticated": True, "auth_enabled": False}
    token = extract_token(request)
    if token and validate_session(token):
        return {"authenticated": True, "auth_enabled": True, "username": config.auth.username}
    return {"authenticated": False, "auth_enabled": True}


@router.put("/password")
async def change_password(payload: PasswordChangePayload, request: Request):
    if not config.auth.enabled:
        return JSONResponse(status_code=400, content={"detail": "认证未开启"})
    token = extract_token(request)
    if not token or not validate_session(token):
        return JSONResponse(status_code=401, content={"detail": "未登录"})
    if not verify_password(payload.old_password, config.auth.password_hash):
        return JSONResponse(status_code=400, content={"detail": "旧密码错误"})
    import bcrypt
    config.auth.password_hash = bcrypt.hashpw(
        payload.new_password.encode(), bcrypt.gensalt()
    ).decode()
    # 持久化到 config.yaml
    from server.config import save_config
    save_config()
    # 清除所有 session，强制重新登录
    from server.core.auth import _sessions
    _sessions.clear()
    return {"ok": True, "message": "密码已修改，请重新登录"}
