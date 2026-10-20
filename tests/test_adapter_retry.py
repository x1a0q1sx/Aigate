# -*- coding: utf-8 -*-
"""失败自动重试（设置页开关，2026-10-20）回归测试。

需求（用户原话）：「设置里加一个功能开关，开启后自动重试，对一个模型请求出错后，
自动重试一次，先不返回报错，如果两次都报错，再返回报错。关闭后保持现状不动。」

锁死的契约：
1. 关闭：完全不包装（工厂原样返回适配器），行为与改动前一致；
2. 开启：非流式失败原样重试一次；两次都失败才抛第二次错误，并带
   `_aigate_retry_exhausted` 标记（阻止 v1 层再叠加重试）；
3. 流式：只有尚未输出实质内容时重试；已输出正文/思考/工具调用后直接抛出；
   重试时丢弃上一轮的元数据缓冲，不向客户端泄漏重复内容；
4. 确定性失败（4xx / 上下文超限 / 每日限额 / 取消）不重试；
5. 适配器内部 `chat_completion → self.stream_chat_completion` 自调用不会
   导致 4 次上游调用（代理只包外层对象）；
6. 后台调用（模型刷新 timeout 非空 / 健康探测）不重试。
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from server.core import adapter_retry as ar
from server.schemas.chat import ChatCompletionRequest


def _req(model="test-model"):
    return ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hi"}])


def _ok(content="ok"):
    return {"id": "chatcmpl-x", "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}


def _content_chunk(text):
    return {"id": "c", "choices": [{"index": 0, "delta": {"content": text}}]}


def _role_chunk():
    return {"id": "c", "choices": [{"index": 0, "delta": {"role": "assistant"}}]}


def _http_error(status):
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url 'http://x'",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(status, request=httpx.Request("POST", "http://x")),
    )


class _FakeAdapter:
    """可编排的适配器替身：chat_script / stream_script 按调用次序取用。

    - chat 项：Exception（抛出）或 dict（返回）
    - stream 项：Exception（迭代前抛出）| list[chunk] | (list[chunk], Exception)
    """

    def __init__(self, chat_script=None, stream_script=None):
        self.chat_script = list(chat_script or [])
        self.stream_script = list(stream_script or [])
        self.chat_calls = 0
        self.stream_calls = 0
        self.timeout = 42
        self.last_proxy_url = None

    async def chat_completion(self, request, api_key, base_url, extra_headers=None):
        self.chat_calls += 1
        item = self.chat_script.pop(0) if self.chat_script else _ok()
        if isinstance(item, BaseException):
            raise item
        return item

    async def stream_chat_completion(self, request, api_key, base_url, extra_headers=None):
        self.stream_calls += 1
        item = self.stream_script.pop(0) if self.stream_script else []
        if isinstance(item, BaseException):
            raise item
        chunks, err = item if isinstance(item, tuple) else (item, None)
        for ck in chunks:
            yield ck
        if err is not None:
            raise err

    async def list_models(self, api_key, base_url, extra_headers=None):
        return []

    async def health_check(self, model, api_key, base_url, extra_headers=None, timeout=10):
        return None


@pytest.fixture(autouse=True)
def _clean_stats():
    ar.reset_retry_stats()
    yield
    ar.reset_retry_stats()


@pytest.fixture
def retry_on(monkeypatch):
    """打开开关（等待时间置 0，测试不睡）。"""
    monkeypatch.setattr(ar, "retry_settings", lambda: (True, 0.0))
    return True


@pytest.fixture
def retry_off(monkeypatch):
    monkeypatch.setattr(ar, "retry_settings", lambda: (False, 0.0))
    return True


# ── 配置默认值 ─────────────────────────────────────────────

def test_retry_config_defaults_off():
    from server.config import Config, RetryConfig
    assert RetryConfig().enabled is False
    assert Config().retry.enabled is False
    assert Config().retry.delay_seconds == 1.0


# ── 关闭态：零行为变化 ─────────────────────────────────────

def test_wrap_adapter_disabled_returns_same_object(retry_off):
    fake = _FakeAdapter()
    assert ar.wrap_adapter(fake) is fake
    assert ar.wrap_free_executor(fake) is fake


def test_disabled_does_not_retry(retry_off):
    fake = _FakeAdapter([httpx.ConnectError("boom"), _ok()])
    adapter = ar.wrap_adapter(fake)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(adapter.chat_completion(_req(), "k", "http://x"))
    assert fake.chat_calls == 1


def test_factory_not_wrapped_when_disabled(retry_off):
    from server.core.model_catalog import create_adapter_for_provider
    from server.adapters.openai_compat import OpenAICompatAdapter
    a = create_adapter_for_provider("openai_compat")
    assert isinstance(a, OpenAICompatAdapter)


# ── 非流式 ────────────────────────────────────────────────

def test_non_stream_retries_once_then_recovers(retry_on):
    fake = _FakeAdapter([httpx.ConnectError("boom"), _ok("second")])
    adapter = ar.wrap_adapter(fake)
    out = asyncio.run(adapter.chat_completion(_req(), "k", "http://x"))
    assert out["choices"][0]["message"]["content"] == "second"
    assert fake.chat_calls == 2
    assert ar.get_retry_stats()["retried"] == 1
    assert ar.get_retry_stats()["recovered"] == 1


def test_non_stream_two_failures_marked_and_raised(retry_on):
    e1, e2 = httpx.ConnectError("first"), httpx.ConnectError("second")
    fake = _FakeAdapter([e1, e2])
    adapter = ar.wrap_adapter(fake)
    with pytest.raises(httpx.ConnectError) as ei:
        asyncio.run(adapter.chat_completion(_req(), "k", "http://x"))
    assert ei.value is e2
    assert getattr(e2, "_aigate_retry_exhausted", False) is True
    assert fake.chat_calls == 2
    assert ar.get_retry_stats()["exhausted"] == 1


def test_non_stream_4xx_not_retried(retry_on):
    fake = _FakeAdapter([_http_error(400), _ok()])
    adapter = ar.wrap_adapter(fake)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.chat_completion(_req(), "k", "http://x"))
    assert fake.chat_calls == 1
    assert ar.get_retry_stats()["skipped"] == 1


def test_non_stream_context_error_not_retried(retry_on):
    fake = _FakeAdapter([RuntimeError("maximum context length is 8192 tokens"), _ok()])
    adapter = ar.wrap_adapter(fake)
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.chat_completion(_req(), "k", "http://x"))
    assert fake.chat_calls == 1


def test_non_stream_daily_quota_not_retried(retry_on):
    msg = ('{"code":6004,"msg":"您的使用量已超出频率限制，将在 2026-10-17 15:26:42 UTC+8 重置，'
           '您也可以切换其他模型继续使用。"}')
    fake = _FakeAdapter([RuntimeError(msg), _ok()])
    adapter = ar.wrap_adapter(fake)
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.chat_completion(_req(), "k", "http://x"))
    assert fake.chat_calls == 1


def test_non_stream_error_result_retried_then_returned(retry_on):
    fake = _FakeAdapter([{"error": "first"}, {"error": "second"}])
    adapter = ar.wrap_adapter(fake)
    out = asyncio.run(adapter.chat_completion(_req(), "k", "http://x"))
    assert out == {"error": "second"}      # 第二次仍失败 → 原样返回错误体
    assert fake.chat_calls == 2


def test_non_stream_cancelled_not_retried(retry_on):
    fake = _FakeAdapter([asyncio.CancelledError(), _ok()])
    adapter = ar.wrap_adapter(fake)

    async def _run():
        try:
            await adapter.chat_completion(_req(), "k", "http://x")
            return "no-raise"
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(_run()) == "cancelled"
    assert fake.chat_calls == 1


# ── 流式 ──────────────────────────────────────────────────

async def _collect(agen):
    out = []
    async for ck in agen:
        out.append(ck)
    return out


def test_stream_retries_before_content(retry_on):
    fake = _FakeAdapter(stream_script=[
        ([_role_chunk()], httpx.ConnectError("boom")),
        [_role_chunk(), _content_chunk("hello")],
    ])
    adapter = ar.wrap_adapter(fake)
    chunks = asyncio.run(_collect(adapter.stream_chat_completion(_req(), "k", "http://x")))
    assert [c["choices"][0]["delta"].get("content") for c in chunks] == [None, "hello"]
    assert fake.stream_calls == 2
    assert ar.get_retry_stats()["retried"] == 1


def test_stream_no_replay_of_metadata_buffer(retry_on):
    """第一轮只产出 role 元数据即失败：重试后客户端只看到第二轮的 role+content。"""
    fake = _FakeAdapter(stream_script=[
        ([_role_chunk()], RuntimeError("upstream_stream_error")),
        [_role_chunk(), _content_chunk("hi")],
    ])
    adapter = ar.wrap_adapter(fake)
    chunks = asyncio.run(_collect(adapter.stream_chat_completion(_req(), "k", "http://x")))
    assert len(chunks) == 2                      # 不是 3（第一轮 role 被丢弃）
    assert chunks[-1]["choices"][0]["delta"]["content"] == "hi"
    assert fake.stream_calls == 2


def test_stream_no_retry_after_content(retry_on):
    fake = _FakeAdapter(stream_script=[
        ([_content_chunk("partial")], httpx.ConnectError("mid-stream boom")),
    ])
    adapter = ar.wrap_adapter(fake)
    seen = []

    async def _run():
        with pytest.raises(httpx.ConnectError):
            async for ck in adapter.stream_chat_completion(_req(), "k", "http://x"):
                seen.append(ck)
    asyncio.run(_run())
    assert seen and seen[0]["choices"][0]["delta"]["content"] == "partial"
    assert fake.stream_calls == 1               # 已输出内容 → 不重试


def test_stream_error_chunk_retried(retry_on):
    fake = _FakeAdapter(stream_script=[
        ([{"error": "upstream exploded"}],),
        [_content_chunk("recovered")],
    ])
    adapter = ar.wrap_adapter(fake)
    chunks = asyncio.run(_collect(adapter.stream_chat_completion(_req(), "k", "http://x")))
    assert chunks[-1]["choices"][0]["delta"]["content"] == "recovered"
    assert fake.stream_calls == 2


def test_stream_two_error_chunks_second_passthrough(retry_on):
    fake = _FakeAdapter(stream_script=[
        [{"error": "first"}],
        [{"error": "second"}],
    ])
    adapter = ar.wrap_adapter(fake)
    chunks = asyncio.run(_collect(adapter.stream_chat_completion(_req(), "k", "http://x")))
    assert chunks == [{"error": "second"}]      # 两次都失败 → 原样透传第二轮错误
    assert fake.stream_calls == 2
    assert ar.get_retry_stats()["exhausted"] == 1


def test_stream_cancelled_not_retried(retry_on):
    fake = _FakeAdapter(stream_script=[asyncio.CancelledError()])
    adapter = ar.wrap_adapter(fake)

    async def _run():
        try:
            await _collect(adapter.stream_chat_completion(_req(), "k", "http://x"))
            return "no-raise"
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(_run()) == "cancelled"
    assert fake.stream_calls == 1


# ── 嵌套 / 工厂 / 后台调用 ─────────────────────────────────

class _AggregatingAdapter(_FakeAdapter):
    """模拟 trae/qoder/codearts/freebuff：非流式由自身流式聚合实现。"""

    async def chat_completion(self, request, api_key, base_url, extra_headers=None):
        self.chat_calls += 1
        parts = []
        async for ck in self.stream_chat_completion(request, api_key, base_url, extra_headers):
            if "error" in ck and "choices" not in ck:
                raise RuntimeError(str(ck["error"]))
            for ch in ck.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    parts.append(d["content"])
        return _ok("".join(parts))


def test_nested_self_call_not_double_retried(retry_on):
    """非流式内部自调用流式时，总上游尝试次数必须是 2（外层一次重试），不是 4。"""
    fake = _AggregatingAdapter(stream_script=[
        httpx.ConnectError("boom"), httpx.ConnectError("boom"),
        httpx.ConnectError("boom"), httpx.ConnectError("boom"),
    ])
    adapter = ar.wrap_adapter(fake)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(adapter.chat_completion(_req(), "k", "http://x"))
    assert fake.chat_calls == 2
    assert fake.stream_calls == 2


def test_factory_wraps_when_enabled(retry_on):
    from server.core.model_catalog import create_adapter_for_provider
    a = create_adapter_for_provider("openai_compat")
    assert isinstance(a, ar.RetryingAdapter)
    assert a.timeout == 180                     # 属性透传
    # 后台刷新（timeout 非空）不包装
    b = create_adapter_for_provider("openai_compat", timeout=20)
    assert not isinstance(b, ar.RetryingAdapter)


def test_free_executor_wrapped_when_enabled(retry_on):
    from server.core import free_providers as fp

    class _FakeExec:
        def __init__(self):
            self.calls = 0

        async def execute_non_stream(self, request):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("boom")
            return {"ok": True}

        async def list_models(self):
            return []

    inner = _FakeExec()
    ex = ar.wrap_free_executor(inner)
    assert isinstance(ex, ar.RetryingFreeExecutor)
    out = asyncio.run(ex.execute_non_stream(_req()))
    assert out == {"ok": True}
    assert inner.calls == 2
    assert asyncio.run(ex.list_models()) == []      # 非推理方法透传
    # 关闭态原样返回（get_free_executor(wrap=False) 语义）
    assert fp.get_free_executor("opencode", wrap=False) is not None


# ── 与 v1 既有重试不叠加 ──────────────────────────────────

def test_retry_budget_zero_when_exhausted():
    from server.api.v1_router import _retry_budget_for
    plain = httpx.ConnectError("x")
    assert _retry_budget_for(plain) == 2        # 既有行为不变
    assert _retry_budget_for(RuntimeError("x")) == 1
    exhausted = httpx.ConnectError("x")
    setattr(exhausted, "_aigate_retry_exhausted", True)
    assert _retry_budget_for(exhausted) == 0    # 自动重试用尽 → 不再叠加


# ── 管理接口 ──────────────────────────────────────────────

def test_retry_endpoints_registered():
    from server.api import admin_ops_router as ops
    paths = {(r.path, method) for r in ops.router.routes
             for method in (r.methods or set())}
    assert ("/admin/api/retry", "GET") in paths
    assert ("/admin/api/retry", "PUT") in paths


def test_retry_endpoint_roundtrip(monkeypatch):
    from server.api import admin_ops_router as ops
    from server.config import RetryConfig
    import server.config as cfg_mod

    cfg = SimpleNamespace(retry=RetryConfig())
    monkeypatch.setattr(cfg_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(cfg_mod, "save_config", lambda: None)

    out = asyncio.run(ops.put_retry_config(ops.RetryModel(enabled=True, delay_seconds=50)))
    assert out["enabled"] is True
    assert out["delay_seconds"] == 30.0         # 钳制到 0~30
    got = asyncio.run(ops.get_retry_config())
    assert got["enabled"] is True and "stats" in got


def test_config_example_documents_retry():
    from pathlib import Path
    text = Path("config.example.yaml").read_text(encoding="utf-8")
    assert "retry:" in text and "enabled: false" in text
