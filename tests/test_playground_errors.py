# -*- coding: utf-8 -*-
"""Playground 错误面回归（2026-09-28 实测事故）。

**事故**：Freebuff 的 glm-5.3-flash 调用报 `HTTP 500: Internal Server Error`，
前端只显示 500，真实原因（Freebucks 额度用尽 / 模型不在此档）只出现在服务端日志。

**根因**：`playground_chat` 的适配器调用点只捕获 httpx 三类异常
（`HTTPStatusError/TimeoutException/ConnectError`）。适配器自抛的**业务异常**
（freebuff 的 `SessionError`、各适配器的 `RuntimeError`）不在其中 → 穿透出
endpoint → FastAPI 兜底成 500。而 `/v1` 同一位置是 `except Exception` 宽捕获
（→ 503 + 可读文案），两条路径口径不一致。

本文件锁死：playground 的**非流式**与**流式**两条路径都必须把适配器业务异常
转成可读错误（503 / SSE error 事件），不得穿透成 500。
"""
import asyncio
from types import SimpleNamespace

import pytest

from server.api import admin_router as adm


# ── 最小 DB 桩：只满足 playground_chat 的查询路径 ────────────────

class _FakeResult:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v

    def scalars(self):
        return self

    def all(self):
        return self._v if isinstance(self._v, list) else ([self._v] if self._v else [])

    def one(self):
        return self._v

    def first(self):
        return self._v


class _FakeDB:
    """playground 直连分支：get(Provider, id) → 服务商；其余查询返回空。"""

    def __init__(self, provider, model):
        self._provider = provider
        self._model = model

    async def get(self, cls, pk):
        return self._provider

    async def execute(self, stmt):
        return _FakeResult(None)

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def close(self):
        return None


def _provider():
    # Freebuff 是 OAuth 服务商 → 走 playground 的 oauth 分支（与生产一致）
    return SimpleNamespace(id=1, name="Freebuff", api_type="freebuff",
                           credential_type="oauth", enabled=True, base_url="",
                           headers=None, oauth_code="freebuff", oauth_owner="__default")


def _model():
    return SimpleNamespace(id=1, provider_id=1, model_id="z-ai/glm-5.3-flash",
                           full_id="Freebuff/z-ai/glm-5.3-flash",
                           input_price=0.0, output_price=0.0,
                           cache_read_input_price=0.0, cache_write_input_price=0.0)


def _install(monkeypatch, adapter, *, api_key="tok"):
    """把 playground 的依赖全部打桩：模型解析、凭据、适配器工厂、日志写入。"""
    async def _get_by_full_id(db, provider_name, model_id):
        return _model()

    monkeypatch.setattr(adm._model_catalog, "get_by_full_id", _get_by_full_id)

    class _Cred:
        ok = True
        api_key = "tok"
        extra_headers = None

    async def _resolve(provider, model, db):
        return _Cred()

    monkeypatch.setattr("server.core.credential_resolver.resolve_credential_async", _resolve)

    import server.core.model_catalog as mc
    monkeypatch.setattr(mc, "create_adapter_for_provider", lambda api_type: adapter)

    async def _noop_log(*a, **k):
        return None

    return _noop_log


class _RaisingAdapter:
    """非流式 / 流式都抛业务异常（模拟 freebuff SessionError）。"""

    def __init__(self, exc):
        self._exc = exc
        self.calls = []

    async def chat_completion(self, request, api_key, base_url, extra_headers=None):
        self.calls.append("chat_completion")
        raise self._exc

    async def stream_chat_completion(self, request, api_key, base_url, extra_headers=None):
        self.calls.append("stream")
        raise self._exc
        yield  # pragma: no cover


class _FakeRequest:
    """足够 playground_chat 使用的 Request 桩（scope/headers/client）。"""

    def __init__(self):
        self.headers = {}
        self.scope = {"headers": [], "client": ("127.0.0.1", 12345)}
        self.client = SimpleNamespace(host="127.0.0.1")


def _body(model="Freebuff/z-ai/glm-5.3-flash", stream=False):
    return SimpleNamespace(model=model, messages=[{"role": "user", "content": "hi"}],
                           stream=stream)


# ── 非流式：业务异常必须变 503 + 可读文案，不得穿透 ──────────────

def test_playground_nonstream_business_error_becomes_readable_503(monkeypatch):
    from server.core.freebuff import SessionError
    exc = SessionError("Freebucks 额度已用尽（本次需 5、余额 0；约 24 分钟后重置）", 429)
    adapter = _RaisingAdapter(exc)
    _install(monkeypatch, adapter)

    resp = asyncio.run(adm.playground_chat(_body(), _FakeRequest(), _FakeDB(_provider(), _model())))

    assert adapter.calls == ["chat_completion"], "必须真的调了适配器"
    assert isinstance(resp, adm.JSONResponse), "不得把异常穿透成 500"
    assert resp.status_code == 503
    payload = resp.body.decode("utf-8")
    assert "Freebucks 额度已用尽" in payload, "原因必须回给前端"
    assert "upstream_call_failed" in payload


def test_playground_nonstream_still_handles_httpx_errors(monkeypatch):
    """原有 httpx 分支不得回归（仍是 503 + upstream_call_failed）。"""
    import httpx
    req = httpx.Request("POST", "https://x")
    exc = httpx.ConnectError("boom", request=req)
    adapter = _RaisingAdapter(exc)
    _install(monkeypatch, adapter)

    resp = asyncio.run(adm.playground_chat(_body(), _FakeRequest(), _FakeDB(_provider(), _model())))
    assert resp.status_code == 503
    assert "ConnectError" in resp.body.decode("utf-8")


# ── 流式：业务异常必须变成 SSE error 事件（而不是中断/500）──────

def _collect_stream(sr):
    async def _run():
        out = b""
        async for piece in sr.body_iterator:
            out += piece if isinstance(piece, bytes) else str(piece).encode()
        return out
    return asyncio.run(_run())


def test_playground_stream_business_error_becomes_sse_error(monkeypatch):
    from server.core.freebuff import SessionError
    exc = SessionError("freebuff 免费层不提供该模型（z-ai/glm-5.3-flash）", 409)
    adapter = _RaisingAdapter(exc)
    _install(monkeypatch, adapter)

    sr = asyncio.run(adm.playground_chat(_body(stream=True), _FakeRequest(),
                                         _FakeDB(_provider(), _model())))
    text = _collect_stream(sr).decode("utf-8", "replace")

    assert "免费层不提供该模型" in text, "SSE 里必须带真实原因"
    assert "[DONE]" in text, "流必须正常收尾（否则前端挂起）"


def test_playground_stream_httpx_error_still_sse(monkeypatch):
    """原有 httpx 分支不得回归。"""
    import httpx
    exc = httpx.ReadTimeout("slow")
    adapter = _RaisingAdapter(exc)
    _install(monkeypatch, adapter)

    sr = asyncio.run(adm.playground_chat(_body(stream=True), _FakeRequest(),
                                         _FakeDB(_provider(), _model())))
    text = _collect_stream(sr).decode("utf-8", "replace")
    assert "ReadTimeout" in text and "[DONE]" in text


# ── 源码守卫：两条路径都必须是宽捕获（防止有人改窄回去）─────────

def test_playground_catches_adapter_business_errors_in_source():
    import inspect
    src = inspect.getsource(adm.playground_chat)
    assert src.count("except Exception as e:") >= 2, \
        "非流式与流式两条适配器调用路径都必须有宽捕获（业务异常不得穿透成 500）"
    assert src.count("upstream_call_failed") >= 2
    assert src.count("upstream_stream_failed") >= 2
