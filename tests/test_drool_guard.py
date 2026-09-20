"""DroolGuard 流口水检测回归测试。"""
import asyncio
import json
import types

import pytest

from server.core import drool_guard as dg


def _cfg(enabled=True, threshold=5, cooldown=30, min_chars=40):
    return types.SimpleNamespace(enabled=enabled, threshold=threshold,
                                 cooldown_minutes=cooldown, min_answer_chars=min_chars)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    dg.reset_state()
    monkeypatch.setattr(dg, "_config", lambda: _cfg())
    yield
    dg.reset_state()


def _answer_chunks(text, cid="x"):
    """模拟流式落库体：chunk 列表（chunk id 每次都不同，内容相同）。"""
    return json.dumps([
        {"id": f"chatcmpl-{cid}", "choices": [{"delta": {"content": t}}]}
        for t in [text[: len(text) // 2], text[len(text) // 2:]]
    ])


LONG = "The input channel is healthy. Let me check if the app is actually processing the taps. " * 2


def test_triggers_after_threshold_with_varying_chunk_ids():
    called = []

    async def fake_penalize(p, m, mins):
        called.append((p, m, mins))
    orig = dg._penalize
    dg._penalize = fake_penalize
    try:
        async def _t():
            for i in range(5):
                dg.note_success("烁公益站", "kimi-k3", _answer_chunks(LONG, cid=f"c{i}"))
            await asyncio.sleep(0.05)
        asyncio.run(_t())
        assert called == [("烁公益站", "kimi-k3", 30)]
    finally:
        dg._penalize = orig


def test_different_answers_reset_chain():
    assert not dg.note_success("p", "m", json.dumps({"choices": [{"message": {"content": LONG + "A"}}]}))
    assert not dg.note_success("p", "m", json.dumps({"choices": [{"message": {"content": LONG + "B"}}]}))
    assert not dg.note_success("p", "m", json.dumps({"choices": [{"message": {"content": LONG + "B"}}]}))
    assert dg.snapshot() == {"p/m": 2}


def test_short_answers_ignored_and_break_chain():
    dg.note_success("p", "m", _answer_chunks(LONG))
    dg.note_success("p", "m", _answer_chunks(LONG))
    dg.note_success("p", "m", "好的")  # 短答复：断链
    assert dg.snapshot() == {}
    dg.note_success("p", "m", _answer_chunks(LONG))
    dg.note_success("p", "m", _answer_chunks(LONG))
    assert dg.snapshot() == {"p/m": 2}  # 重新从 2 计


def test_disabled_no_effect():
    dg._config = lambda: _cfg(enabled=False)
    for i in range(6):
        dg.note_success("p", "m", _answer_chunks(LONG, cid=str(i)))
    assert dg.snapshot() == {}


def test_non_stream_dict_body_supported():
    body = {"choices": [{"message": {"role": "assistant", "content": LONG}}], "usage": {}}
    called = []

    async def fake_penalize(p, m, mins):
        called.append((p, m, mins))
    dg._penalize = fake_penalize

    async def _t():
        for i in range(4):
            dg.note_success("p", "m", json.dumps(body))
            await asyncio.sleep(0)
        assert dg.snapshot() == {"p/m": 4}
        dg.note_success("p", "m", json.dumps(body))  # 第 5 次触发
        await asyncio.sleep(0.05)
    asyncio.run(_t())
    assert called == [("p", "m", 30)]
