# -*- coding: utf-8 -*-
"""OpenCode CLI sidecar 桥接：消息扁平化 / 响应转换 / 可用性探测

背景（2026-09-22 取证）：opencode.ai 免费层只认「官方 CLI 进程建立的会话」，
纯 HTTP 转发一律 403。方案 = 服务器常驻官方 CLI（`opencode serve`），AIGate 通过
其 HTTP API 转发（复刻 9router「让真 CLI 当上游客户端」的架构）。
"""
import pytest

from server.core import opencode_sidecar as sc


def test_flatten_messages_preserves_roles():
    out = sc.flatten_messages([
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在"},
        {"role": "user", "content": "继续"},
    ])
    assert "[system]\nSYS" in out
    assert "你好" in out and "在" in out and "继续" in out
    # 行为语义由 sidecar 的 aigate agent 提示词承担，这里不再叠加前置指令
    assert not out.startswith("You are the assistant")


def test_flatten_messages_typed_blocks():
    out = sc.flatten_messages([
        {"role": "user", "content": [
            {"type": "text", "text": "A"},
            {"type": "image_url", "image_url": {"url": "x"}},   # 非文本块忽略
            {"type": "text", "text": "B"},
        ]},
    ])
    assert "AB" in out


def test_flatten_messages_empty():
    assert sc.flatten_messages([]) == ""
    assert sc.flatten_messages([{"role": "user", "content": ""}]) == ""


def test_content_to_text_splits_reasoning_and_text():
    text, reasoning = sc._content_to_text([
        {"type": "reasoning", "text": "想一想"},
        {"type": "text", "text": "答案"},
        {"type": "text", "text": "！"},
    ])
    assert text == "答案！"
    assert reasoning == "想一想"


def test_to_openai_response_shape():
    msg = {
        "id": "msg_1", "type": "assistant",
        "time": {"created": 1790062450043, "completed": 1790062450359},
        "model": {"id": "mimo-v2.6-flash-free", "providerID": "opencode"},
        "content": [{"type": "reasoning", "text": "r"},
                    {"type": "text", "text": "ok"}],
        "finish": "stop",
        "tokens": {"input": 112, "output": 3, "reasoning": 15},
    }
    resp = sc._to_openai_response(msg, "mimo-v2.6-flash-free", "p")
    assert resp["object"] == "chat.completion"
    assert resp["model"] == "mimo-v2.6-flash-free"
    ch = resp["choices"][0]
    assert ch["message"]["content"] == "ok"
    assert ch["message"]["reasoning_content"] == "r"
    assert ch["finish_reason"] == "stop"
    assert resp["usage"] == {"prompt_tokens": 112, "completion_tokens": 3,
                             "total_tokens": 115}


@pytest.mark.asyncio
async def test_await_assistant_waits_for_completion(monkeypatch):
    """关键回归：assistant 消息是逐步填充的，必须等 time.completed 才返回，
    否则会拿到「只有空 reasoning 的中间态」→ 空回复。"""
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, items):
            self.status_code = 200
            self._items = items
        def json(self):
            return {"data": self._items}

    class FakeClient:
        async def get(self, url):
            calls["n"] += 1
            if calls["n"] < 3:
                # 中间态：有 assistant 但无 completed，content 里 text 为空
                return FakeResp([{
                    "type": "assistant", "id": "m1",
                    "time": {"created": 2000},
                    "content": [{"type": "reasoning", "text": ""}],
                }])
            return FakeResp([{
                "type": "assistant", "id": "m1",
                "time": {"created": 2000, "completed": 2100},
                "content": [{"type": "text", "text": "完成"}],
                "finish": "stop",
            }])

    msg, err = await sc._await_assistant(FakeClient(), "sid", since_ms=1000,
                                         poll_interval=0.01, deadline_s=2)
    assert err is None
    assert calls["n"] >= 3, "必须轮询到 completed 才返回"
    assert sc._content_to_text(msg["content"])[0] == "完成"


@pytest.mark.asyncio
async def test_await_assistant_surfaces_error():
    class FakeResp:
        status_code = 200
        def json(self):
            return {"data": [{
                "type": "assistant", "id": "m1",
                "time": {"created": 2000, "completed": 2001},
                "error": {"message": "upstream 403"},
            }]}

    class FakeClient:
        async def get(self, url):
            return FakeResp()

    msg, err = await sc._await_assistant(FakeClient(), "sid", since_ms=1000,
                                         poll_interval=0.01, deadline_s=2)
    assert err and "403" in str(err)


@pytest.mark.asyncio
async def test_free_executor_opencode_uses_sidecar(monkeypatch):
    """opencode 免费执行器必须走 sidecar（纯 HTTP 已被上游封锁）"""
    from server.core import free_providers as fp
    called = {}

    async def fake_alive():
        return True

    async def fake_chat(messages, model_id, provider_id="opencode", **kw):
        called["model"] = model_id
        called["provider"] = provider_id
        return {"choices": [{"message": {"role": "assistant", "content": "x"}}]}

    monkeypatch.setattr(sc, "sidecar_alive", fake_alive)
    monkeypatch.setattr(sc, "chat_completion", fake_chat)

    ex = fp.get_free_executor("opencode")
    req = fp.ChatCompletionRequest(model="mimo-v2.6-flash-free",
                                   messages=[{"role": "user", "content": "hi"}],
                                   stream=False)
    out = await ex.execute_non_stream(req)
    assert called.get("model") == "mimo-v2.6-flash-free"
    assert out["choices"][0]["message"]["content"] == "x"


@pytest.mark.asyncio
async def test_free_executor_opencode_errors_when_sidecar_down(monkeypatch):
    from server.core import free_providers as fp

    async def dead():
        return False

    monkeypatch.setattr(sc, "sidecar_alive", dead)
    ex = fp.get_free_executor("opencode")
    req = fp.ChatCompletionRequest(model="m-x",
                                   messages=[{"role": "user", "content": "hi"}],
                                   stream=False)
    with pytest.raises(RuntimeError) as ei:
        await ex.execute_non_stream(req)
    assert "sidecar 未运行" in str(ei.value)
