# -*- coding: utf-8 -*-
"""Playground 请求日志完整性回归（2026-10-25 生产实证）。

**事故**：用户在「分析 → 请求日志」里看不到 Playground 的任何请求。

**根因**：`playground_chat` 的流式分支只有两条 except 里的 `_write_log("error", ...)`，
**成功路径完全没有日志调用点**。而前端 Playground 每次都发 `stream: true`
（`Playground.vue: streamRun(run, {..., stream: true})`）——所以只要模型调通，
这次调用在日志页/统计/用量分析里就彻底不存在；只有失败才留痕。
生产 A/B 实证（同一模型、24h 内无其它流量）：流式 0 行 / 非流式 1 行。

顺带锁定两件事：
1. free_tier 分支此前从不设 `route_result` → 日志 `routed_provider`/`routed_provider_id`
   恒 NULL，分析页把这批请求全丢进「(未归属)」桶。
2. 流式 usage 只在末块给出，必须从 chunk 里取（resp_dict 是 None），
   否则 token/成本统计恒 0。
"""
import asyncio
from types import SimpleNamespace

from server.api import admin_router as adm


# ── 桩件 ──────────────────────────────────────────────────────

class _Captured:
    """收集 write_log 收到的 kwargs。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, db, **kwargs):
        self.calls.append(kwargs)
        return 1


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def close(self):
        return None


class _FakeResult:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v

    def scalars(self):
        return self

    def all(self):
        return []

    def one(self):
        return self._v


class _FakeDB:
    def __init__(self, provider):
        self._provider = provider

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


class _FakeRequest:
    def __init__(self):
        self.headers = {}
        self.scope = {"headers": [], "client": ("127.0.0.1", 12345)}
        self.client = SimpleNamespace(host="127.0.0.1")


def _provider(cred="oauth", name="Freebuff", api_type="freebuff"):
    return SimpleNamespace(id=7, name=name, api_type=api_type, credential_type=cred,
                           enabled=True, base_url="", headers=None,
                           oauth_code="freebuff", oauth_owner="__default")


def _model():
    return SimpleNamespace(id=3, provider_id=7, model_id="z-ai/glm-5.3-flash",
                           full_id="Freebuff/z-ai/glm-5.3-flash",
                           input_price=1.0, output_price=2.0,
                           cache_read_input_price=0.5, cache_write_input_price=0.5)


class _OkAdapter:
    """流式返回带 usage 的末块；非流式返回标准响应。"""

    def __init__(self):
        self.calls = []

    async def stream_chat_completion(self, request, api_key, base_url, extra_headers=None):
        self.calls.append("stream")
        yield {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}
        yield {"choices": [{"index": 0, "delta": {"content": "ok"}}]}
        yield {"choices": [{"index": 0, "delta": {"content": "!"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                         "prompt_tokens_details": {"cached_tokens": 40}}}

    async def chat_completion(self, request, api_key, base_url, extra_headers=None):
        self.calls.append("nonstream")
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok!"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


def _install(monkeypatch, adapter, captured):
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

    # _write_log 内部 `from server.db import AsyncSessionLocal`、playground_chat 内部
    # `from server.core.request_logger import write_log` → 都在调用时导入，patch 模块属性即可。
    import server.db as sdb
    import server.core.request_logger as rl
    monkeypatch.setattr(sdb, "AsyncSessionLocal", _FakeSession)
    monkeypatch.setattr(rl, "write_log", captured)

    async def _note(*a, **k):
        return None

    monkeypatch.setattr("server.core.credential_resolver.note_account_result", _note)


def _body(stream):
    return SimpleNamespace(model="Freebuff/z-ai/glm-5.3-flash",
                           messages=[{"role": "user", "content": "hi"}], stream=stream)


def _drain(sr):
    async def _run():
        out = b""
        async for piece in sr.body_iterator:
            out += piece if isinstance(piece, bytes) else str(piece).encode()
        return out
    return asyncio.run(_run())


# ── 核心：流式成功必须落日志 ──────────────────────────────────

def test_stream_success_writes_log(monkeypatch):
    captured = _Captured()
    adapter = _OkAdapter()
    _install(monkeypatch, adapter, captured)

    sr = asyncio.run(adm.playground_chat(_body(stream=True), _FakeRequest(),
                                         _FakeDB(_provider())))
    text = _drain(sr).decode("utf-8", "replace")

    assert "[DONE]" in text, "流本身必须正常收尾"
    assert adapter.calls == ["stream"]
    assert len(captured.calls) == 1, (
        "流式成功必须写一条日志；否则日志页/用量分析里整条调用凭空消失（2026-10-25 事故）")

    rec = captured.calls[0]
    assert rec["status"] == "success"
    assert rec["routed_provider"] == "Freebuff", "必须归因到服务商"
    assert rec["routed_provider_id"] == 7, "routed_provider_id 必须一起写（用量聚合靠它）"
    assert rec["routed_model"] == "z-ai/glm-5.3-flash"
    assert rec["prompt_tokens"] == 100
    assert rec["completion_tokens"] == 20
    assert rec["cache_read_tokens"] == 40, "缓存读要进统计（否则命中率恒 0）"
    assert rec["ttft_ms"] is not None, "首字延迟要记录"
    assert rec["estimated_cost_usd"] > 0, "有单价时必须算成本"


def test_nonstream_success_still_writes_log(monkeypatch):
    """非流式既有行为不得回归。"""
    captured = _Captured()
    adapter = _OkAdapter()
    _install(monkeypatch, adapter, captured)

    resp = asyncio.run(adm.playground_chat(_body(stream=False), _FakeRequest(),
                                           _FakeDB(_provider())))
    assert isinstance(resp, dict)
    assert len(captured.calls) == 1
    rec = captured.calls[0]
    assert rec["status"] == "success"
    assert rec["routed_provider"] == "Freebuff"
    assert rec["prompt_tokens"] == 100


def test_stream_error_also_writes_log(monkeypatch):
    """失败路径原本就有日志，加了成功路径后不能把它挤掉。"""
    captured = _Captured()

    class _Boom:
        async def stream_chat_completion(self, *a, **k):
            raise RuntimeError("upstream exploded")
            yield  # pragma: no cover

    _install(monkeypatch, _Boom(), captured)
    sr = asyncio.run(adm.playground_chat(_body(stream=True), _FakeRequest(),
                                         _FakeDB(_provider())))
    _drain(sr)

    assert len(captured.calls) == 1
    assert captured.calls[0]["status"] == "error"
    assert "upstream exploded" in captured.calls[0]["error_msg"]


# ── free_tier：必须归因，不能进「(未归属)」桶 ─────────────────

def test_free_tier_stream_records_provider(monkeypatch):
    """free_tier 分支此前不设 route_result → routed_provider 恒 NULL（未归属桶）。"""
    captured = _Captured()

    class _FreeExec:
        async def execute_stream(self, req):
            yield {"choices": [{"index": 0, "delta": {"content": "hi"}}]}

    import server.core.free_providers as fp
    monkeypatch.setattr(fp, "get_free_executor", lambda code: _FreeExec())
    monkeypatch.setattr(fp, "resolve_free_code", lambda name, oauth_code: "freebuff")
    monkeypatch.setattr(adm._model_catalog, "get_by_full_id",
                        lambda db, p, m: _async_return(_model()))

    import server.db as sdb
    import server.core.request_logger as rl
    monkeypatch.setattr(sdb, "AsyncSessionLocal", _FakeSession)
    monkeypatch.setattr(rl, "write_log", captured)

    prov = _provider(cred="free_tier")
    body = SimpleNamespace(model="Freebuff/z-ai/glm-5.3-flash",
                           messages=[{"role": "user", "content": "hi"}], stream=True)
    sr = asyncio.run(adm.playground_chat(body, _FakeRequest(), _FakeDB(prov)))
    _drain(sr)

    assert len(captured.calls) == 1
    rec = captured.calls[0]
    assert rec["status"] == "success"
    assert rec["routed_provider"] == "Freebuff", "free_tier 也必须归因到服务商"
    assert rec["routed_provider_id"] == 7


async def _async_return(v):
    return v


# ── 源码守卫：防止有人把成功路径的日志删回去 ──────────────────

def test_stream_branch_has_success_log_in_source():
    import inspect
    src = inspect.getsource(adm.playground_chat)
    assert src.count('_write_log("success"') >= 3, (
        "free_tier 流式/非流式 + 主流程非流式 + 主流程流式都必须有成功日志调用点")
    # 主流程流式分支里的那次，必须带 usage_snapshot（流式 usage 只出现在 chunk 里）
    assert "usage_snapshot=_usage_snap" in src
    assert "ttft_ms=_ttft_ms" in src


def test_write_log_failure_is_visible_in_logs():
    """写日志失败不能再静默 pass —— 否则线上缺日志无从发现。"""
    import inspect
    src = inspect.getsource(adm.playground_chat)
    inner = src[src.index("async def _write_log"):]
    inner = inner[:inner.index("\n    if not is_auto:")]
    assert "logger.warning" in inner, "异常分支必须留下可查的 warning"
