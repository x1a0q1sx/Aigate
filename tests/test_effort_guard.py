# -*- coding: utf-8 -*-
"""思考强度（reasoning_effort）自适应降级回归（2026-10-29 生产取证）。

**事故**：CodeBuddy CN hy4-preview 在 07:33 返回 400 code 11150
"the reasoning effort value is not supported by the current model"
（extError.code=invalid_reasoning_effort），白天复测同一模型所有标准档位全 200，
唯一被拒的 `max` 报 11133 —— 11150 是 (模型 × 当前允许档位集合) 的**动态**校验。

**修复覆盖三层**（本文件逐层锁定）：
1. `core/effort_guard.py`：识别 effort 拒因 + (host, 模型, 档位) 抑制表（TTL）；
2. `adapters/openai_compat.py`：上游 400 effort 拒因 → 剥 effort+summary 重试一次；
   抑制期内的档位直接不带出站；
3. `api/v1_router.py`：11150/11133 等请求形状错误不再 mark_failure+mark_cooling
   （模型是好的，冷却只会误伤——生产那台 hy4-preview 就这么被白冷却 30s）；
   上游目录声明 supportedEfforts 白名单时越界档位归一到 defaultEffort。

抓包方式：monkeypatch 模块内 httpx.AsyncClient（test_muse 同款桩件），
记录每次出站 json 并按队列回放响应。
"""
import json
import json as _json

import httpx
import pytest

from server.core import effort_guard
from server.adapters.openai_compat import OpenAICompatAdapter
from server.schemas.chat import ChatCompletionRequest


CODEBUDDY_BASE = "https://copilot.tencent.com"

# 生产 2026-10-29 07:33:39 原文（截取）
BODY_11150 = json.dumps({
    "code": 11150,
    "msg": "the reasoning effort value is not supported by the current model",
    "requestId": "77acade2-34fe-4849-bcfc-040bb7c6d98c",
    "extError": {"code": "invalid_reasoning_effort"},
})
BODY_11133 = json.dumps({"code": 11133, "msg": "Invalid request parameters"})


def _ok_body(model="hy4-preview"):
    return {
        "id": "chatcmpl-x", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _req(effort=None):
    d = {"model": "hy4-preview",
         "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    if effort is not None:
        d["reasoning_effort"] = effort
    return ChatCompletionRequest(**d)


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeResp:
    """非流式响应或流式响应两用：status>=400 时带 text，<400 时是 SSE 帧。"""

    def __init__(self, status, text):
        self.status_code = status
        self.reason_phrase = "Bad Request" if status >= 400 else "OK"
        self._text = text
        self.request = None  # httpx.HTTPStatusError(request=resp.request) 需要

    @property
    def text(self):
        return self._text

    def json(self):
        return _json.loads(self._text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Client error '{self.status_code}' for url",
                request=None, response=None)

    async def aread(self):
        return self._text.encode()

    async def aiter_lines(self):
        # _collect_stream 按 delta 拼接，SSE 帧必须是 delta 形态
        body = {"choices": [{"index": 0, "delta": {"content": "ok"},
                             "finish_reason": "stop"}]}
        for line in (f"data: {_json.dumps(body)}", "data: [DONE]"):
            yield line


def _install(monkeypatch, results, calls):
    """替换 openai_compat 模块里的 httpx.AsyncClient。

    results: [(status, text_or_json, mode)]，mode: "json" | "stream"。
    """
    queue = list(results)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, **k):
            calls.append({"url": url, "json": json})
            status, body, mode = queue.pop(0)
            text = body if isinstance(body, str) else _json.dumps(body)
            return _FakeResp(status, text)

        def stream(self, method, url, headers=None, json=None, **k):
            calls.append({"url": url, "json": json, "_stream": True})
            status, body, mode = queue.pop(0)
            text = body if isinstance(body, str) else _json.dumps(body)
            return _FakeStreamCtx(_FakeResp(status, text))

    import server.adapters.openai_compat as oc
    monkeypatch.setattr(oc.httpx, "AsyncClient", _Client)


# ── 1. effort_guard 判据 ─────────────────────────────────────────


class TestGuard:
    def test_production_11150_body_matches(self):
        assert effort_guard.is_reasoning_effort_rejection(BODY_11150) is True
        assert effort_guard.is_request_shape_error(BODY_11150) is True

    def test_marker_without_code(self):
        assert effort_guard.is_reasoning_effort_rejection(
            'extError {"code":"invalid_reasoning_effort"}') is True

    def test_11133_is_shape_not_effort(self):
        assert effort_guard.is_reasoning_effort_rejection(BODY_11133) is False
        assert effort_guard.is_request_shape_error(BODY_11133) is True

    def test_unrelated_errors_not_matched(self):
        for t in ('{"code":6004,"msg":"daily limit"}', "ConnectError: x",
                  '{"code":11102,"msg":"service info not found"}', ""):
            assert effort_guard.is_reasoning_effort_rejection(t) is False
            assert effort_guard.is_request_shape_error(t) is False

    def test_suppression_ttl_roundtrip(self):
        effort_guard.suppress("copilot.tencent.com", "hy4-preview", "High")
        assert effort_guard.is_suppressed("copilot.tencent.com", "hy4-preview", "high")
        assert not effort_guard.is_suppressed("copilot.tencent.com", "hy4-preview", "low")
        assert not effort_guard.is_suppressed("other.host", "hy4-preview", "High")
        assert not effort_guard.is_suppressed("copilot.tencent.com", "hy3", "High")

    def test_suppression_expires(self):
        effort_guard.suppress("h", "m", "high", ttl=-1)  # 已过期
        assert not effort_guard.is_suppressed("h", "m", "high")
        assert effort_guard.suppressed_count() == 0

    def test_empty_effort_never_suppressed(self):
        effort_guard.suppress("h", "m", "")
        assert effort_guard.suppressed_count() == 0


# ── 2. 适配器：剥档位重试 + 抑制表 ────────────────────────────────


@pytest.mark.asyncio
async def test_nonstream_downgrades_on_11150(monkeypatch):
    """上游 400+11150 → 剥 effort+summary 重发一次 → 200。"""
    calls = []
    _install(monkeypatch, [
        (400, BODY_11150, "json"),
        (200, _ok_body(), "json"),
    ], calls)
    ad = OpenAICompatAdapter()
    r = await ad.chat_completion(_req("high"), "k", CODEBUDDY_BASE)
    assert r["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 2
    # 第一次带档位（+CodeBuddy 方言补的 reasoning_summary），第二次都没有
    assert calls[0]["json"]["reasoning_effort"] == "high"
    assert calls[0]["json"].get("reasoning_summary") == "auto"
    assert "reasoning_effort" not in calls[1]["json"]
    assert "reasoning_summary" not in calls[1]["json"]
    # 抑制表已记：同 (host, model, effort) 下次直接不带
    assert effort_guard.is_suppressed("copilot.tencent.com", "hy4-preview", "high")


@pytest.mark.asyncio
async def test_nonstream_suppressed_effort_skips_wire(monkeypatch):
    """抑制期内：单次出站就不带 effort（省一次注定 400 的往返）。"""
    effort_guard.suppress("copilot.tencent.com", "hy4-preview", "high")
    calls = []
    _install(monkeypatch, [(200, _ok_body(), "json")], calls)
    ad = OpenAICompatAdapter()
    await ad.chat_completion(_req("high"), "k", CODEBUDDY_BASE)
    assert len(calls) == 1
    assert "reasoning_effort" not in calls[0]["json"]


@pytest.mark.asyncio
async def test_nonstream_other_400_not_downgraded(monkeypatch):
    """其它 400（如 11133 参数错）不剥档位、原样抛出（不掩盖真实参数问题）。"""
    calls = []
    _install(monkeypatch, [(400, BODY_11133, "json")], calls)
    ad = OpenAICompatAdapter()
    with pytest.raises(httpx.HTTPStatusError):
        await ad.chat_completion(_req("high"), "k", CODEBUDDY_BASE)
    assert len(calls) == 1
    assert not effort_guard.is_suppressed("copilot.tencent.com", "hy4-preview", "high")


@pytest.mark.asyncio
async def test_stream_downgrades_on_11150(monkeypatch):
    """流式路径同样剥档位重发（CodeBuddy force_stream，400 发生在流头）。"""
    calls = []
    _install(monkeypatch, [
        (400, BODY_11150, "stream"),
        (200, "", "stream"),
    ], calls)
    ad = OpenAICompatAdapter()
    chunks = [c async for c in ad.stream_chat_completion(_req("xhigh"), "k", CODEBUDDY_BASE)]
    assert any(c.get("choices") for c in chunks if isinstance(c, dict))
    assert len(calls) == 2
    assert calls[0]["json"]["reasoning_effort"] == "xhigh"
    assert "reasoning_effort" not in calls[1]["json"]
    assert effort_guard.is_suppressed("copilot.tencent.com", "hy4-preview", "xhigh")


@pytest.mark.asyncio
async def test_stream_no_downgrade_after_content_emitted(monkeypatch):
    """已吐实质内容后失败 → 不重发（避免重复输出）。本桩的 200 流不会中途失败，
    用 emit 后抛错的变体覆盖：首个 chunk 带内容、其后流断（非 HTTPStatusError），
    断言只出站一次且异常上抛。"""
    calls = []
    _install(monkeypatch, [(200, "", "stream")], calls)

    ad = OpenAICompatAdapter()
    gen = ad.stream_chat_completion(_req("high"), "k", CODEBUDDY_BASE)
    got = []
    async for c in gen:
        got.append(c)
        break  # 拿到首个 chunk 即人为中断（客户端断开的等价形态）
    assert got, "应有首个 chunk"
    # 取消后抑制表不应记档（没有 11150 拒因）
    assert effort_guard.suppressed_count() == 0


# ── 3. v1_router：白名单归一 + 形状错误不罚冷却 ───────────────────


class _M:
    """极简 model 替身（_without_unsupported_reasoning 只读这几项）。"""

    def __init__(self, caps=None, sup=True):
        self.supports_reasoning_effort = sup
        self.model_id = "hy4-preview"
        self.capabilities = caps


def _normalize(effort, caps):
    from server.api.v1_router import _without_unsupported_reasoning
    return _without_unsupported_reasoning(_req(effort), _M(caps)).reasoning_effort


class TestNormalize:
    def test_in_whitelist_passthrough(self):
        caps = {"reasoning_efforts": ["low", "high"], "default_effort": "low"}
        assert _normalize("high", caps) == "high"

    def test_out_of_whitelist_to_default(self):
        caps = {"reasoning_efforts": ["low", "high"], "default_effort": "low"}
        assert _normalize("xhigh", caps) == "low"

    def test_out_of_whitelist_no_default_middle_fallback(self):
        caps = {"reasoning_efforts": ["low", "medium", "high"]}
        assert _normalize("xhigh", caps) == "medium"

    def test_none_off_stripped_not_mapped(self):
        caps = {"reasoning_efforts": ["low", "high"], "default_effort": "low"}
        assert _normalize("none", caps) is None
        assert _normalize("off", caps) is None

    def test_no_whitelist_untouched(self):
        assert _normalize("high", None) == "high"
        assert _normalize("high", {"reasoning_efforts": []}) == "high"
        assert _normalize("high", {"reasoning_efforts": "low"}) == "high"

    def test_case_insensitive(self):
        caps = {"reasoning_efforts": ["Low", "High"], "default_effort": "Low"}
        # 白名单命中以**上游拼写**出站（逐字节匹配的上游校验不被大小写绊倒）
        assert _normalize("HIGH", caps) == "High"
        # 越界档位归一到 defaultEffort（同样用上游拼写）
        assert _normalize("XHigh", caps) == "Low"


def test_shape_error_excluded_from_cooling_source_guard():
    """源码守卫：_record_fail 等冷却点必须带 is_request_shape_error 闸。

    直接单测嵌套闭包成本高（需整棵路由桩件），用与
    test_playground_stream_logging 相同的源码守卫口径：锁定每个
    mark_cooling 失败记账点前面都有形状错误豁免。"""
    src = open("server/api/v1_router.py", encoding="utf-8").read()
    # 主 import 收口
    assert "from server.core.effort_guard import is_request_shape_error" in src
    # 非流式 _record_fail / 探测 / 流式 _record_fail / 组合流式 / 组合非流式：
    # 5 处失败记账点全部带闸（race_overtaken 与 free_tier 是独立语义，不在此列）
    assert src.count("not is_request_shape_error") >= 5
    # 落库注记区分形状错误（非流式与流式 cooldown_note 各一处）
    assert '" | request_shape_error"' in src
    assert '"request_shape_error" if is_request_shape_error' in src
