"""OAuth 回调落地 /providers/oauth 隐藏页面 + auth 中间件豁免回归测试。

对应审计 P0-2：/admin/oauth/callback 曾被 AuthMiddleware 拦成 SPA index.html，
浏览器授权流全断。现精确豁免，回调结果自动重定向到 OAuth 连接页。
"""
import asyncio
from urllib.parse import unquote

import pytest

from server.core.auth import _PUBLIC_PATHS, _is_admin_api_path
from server.api.oauth_router import oauth_callback


def test_oauth_admin_endpoints_are_api_not_spa_page():
    # 未认证时必须返回 401 JSON，而非 200+index.html（否则前端 JSON.parse 炸）
    assert _is_admin_api_path("/admin/oauth/providers")
    assert _is_admin_api_path("/admin/oauth/connections")
    assert _is_admin_api_path("/admin/api/health")
    assert not _is_admin_api_path("/admin")
    assert not _is_admin_api_path("/admin/api")  # 精确登录页路径另行豁免
    assert not _is_admin_api_path("/providers/oauth")


class _FakeClient:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def exchange_code_for_token(self, provider_code="", code="", state="", db=None):
        self.calls.append(("exchange", code, state))
        return self._result

    async def complete_pending(self, code, db):
        self.calls.append(("complete", code))
        return self._result


def _patch_client(monkeypatch, result):
    fake = _FakeClient(result)
    import server.api.oauth_router as m
    monkeypatch.setattr(m, "get_oauth_client", lambda: fake)
    return fake


def test_callback_is_auth_exempt():
    # 浏览器回调导航不可能带 Authorization 头，必须精确豁免
    assert "/admin/oauth/callback" in _PUBLIC_PATHS


def test_callback_success_redirects_to_oauth_page(monkeypatch):
    fake = _patch_client(monkeypatch, (True, "ok", {"access_token": "t"}))
    resp = asyncio.run(oauth_callback(code="abc", state="cline|__default|x", db=None))
    assert resp.status_code == 302
    assert resp.headers["location"] == "/providers/oauth?oauth=success"
    assert ("exchange", "abc", "cline|__default|x") in fake.calls


def test_callback_stateless_uses_complete_pending(monkeypatch):
    fake = _patch_client(monkeypatch, (True, "ok", {}))
    resp = asyncio.run(oauth_callback(code="xyz", state="", db=None))
    assert resp.headers["location"] == "/providers/oauth?oauth=success"
    assert ("complete", "xyz") in fake.calls


def test_callback_failure_lands_with_error_msg(monkeypatch):
    _patch_client(monkeypatch, (False, "token endpoint HTTP 400: bad code", None))
    resp = asyncio.run(oauth_callback(code="abc", state="", db=None))
    assert resp.headers["location"].startswith("/providers/oauth?oauth=error")
    assert "HTTP+400" in resp.headers["location"] or "HTTP%20400" in resp.headers["location"]
    assert "bad code" in unquote(resp.headers["location"])


def test_callback_exchange_exception_does_not_leak_500(monkeypatch):
    class _Boom(_FakeClient):
        async def complete_pending(self, code, db):
            raise RuntimeError("connect timeout")

    fake = _Boom(None)
    import server.api.oauth_router as m
    monkeypatch.setattr(m, "get_oauth_client", lambda: fake)
    resp = asyncio.run(oauth_callback(code="abc", state="", db=None))
    assert resp.headers["location"].startswith("/providers/oauth?oauth=error")
    assert "connect timeout" in unquote(resp.headers["location"])


def test_callback_user_denied_error_param(monkeypatch):
    fake = _patch_client(monkeypatch, (True, "ok", {}))
    resp = asyncio.run(oauth_callback(code="", state="", error="access_denied", db=None))
    assert resp.headers["location"].startswith("/providers/oauth?oauth=error")
    assert "access_denied" in unquote(resp.headers["location"])
    assert fake.calls == []  # 拒绝回跳不应尝试换票


def test_callback_missing_code_and_error_is_400(monkeypatch):
    _patch_client(monkeypatch, (True, "ok", {}))
    resp = asyncio.run(oauth_callback(code="", state="", db=None))
    assert resp.status_code == 400
