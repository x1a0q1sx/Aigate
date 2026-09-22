# -*- coding: utf-8 -*-
"""流式分片合并回归（`_consolidate_openai_stream`）。

背景（2026-09-22 用户报告「思考在 ZCode 里每个字符单独一行」）：
上游 CodeBuddy CN 逐字下发（每个 chunk 1~2 个字符），且**每个** chunk 的 delta 里都带
`"tool_calls": []`。合并器早先只判 `if tc is not None`，把空列表也当成「有工具调用」，
于是每个 chunk 都 flush 一次缓冲 → 网关出站流退化成逐字符，客户端一行一个字符。

判据：合并后的分片应达到 content_chunk_size；reasoning 同样按阈值合并。
"""
import asyncio

import pytest

from server.adapters.openai_compat import _consolidate_openai_stream


def _collect(chunks, drop_reasoning=False, chunk_size=24):
    """跑一遍合并器，返回 (reasoning 分片长度, content 分片长度)。"""
    async def _run():
        r, c = [], []
        async for ck in _consolidate_openai_stream(chunks, drop_reasoning, chunk_size):
            for ch in (ck.get("choices") or []):
                d = ch.get("delta") or {}
                if isinstance(d.get("reasoning_content"), str) and d["reasoning_content"]:
                    r.append(len(d["reasoning_content"]))
                if isinstance(d.get("content"), str) and d["content"]:
                    c.append(len(d["content"]))
        return r, c
    return asyncio.run(_run())


def _char_stream(text, *, empty_tool_calls=False, reasoning=None):
    """模拟逐字符上游；empty_tool_calls=True 复刻 CodeBuddy 的 delta 形态。"""
    async def gen():
        if reasoning:
            for ch in reasoning:
                d = {"content": "", "reasoning_content": ch}
                if empty_tool_calls:
                    d["tool_calls"] = []
                    d["function_call"] = None
                    d["refusal"] = ""
                yield {"id": "c1", "created": 1, "model": "m",
                       "choices": [{"index": 0, "delta": d, "finish_reason": None}]}
        for ch in text:
            d = {"content": ch, "reasoning_content": ""}
            if empty_tool_calls:
                d["tool_calls"] = []
                d["function_call"] = None
                d["refusal"] = ""
            yield {"id": "c1", "created": 1, "model": "m",
                   "choices": [{"index": 0, "delta": d, "finish_reason": None}]}
        yield {"id": "c1", "created": 1, "model": "m",
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return gen()


def test_empty_tool_calls_must_not_bypass_buffer():
    """核心回归：delta 里的 `tool_calls: []` 是「没有工具调用」，必须继续缓冲。

    修复前：30 个 1 字符分片（逐字符）；修复后：按 24 字符合并。
    """
    text = "二分查找是在有序数组中查找目标值的算法，时间复杂度为对数级。"
    _, with_empty = _collect(_char_stream(text, empty_tool_calls=True))
    _, without = _collect(_char_stream(text, empty_tool_calls=False))
    assert len(with_empty) <= 4, "带空 tool_calls 也应按阈值合并，实际分片=%s" % with_empty
    assert max(with_empty) >= 11, "分片不应退化成逐字符: %s" % with_empty
    assert len(with_empty) == len(without), "空 tool_calls 与不带该字段应等价"


def test_reasoning_consolidated_by_threshold():
    """思考流同样按阈值合并（修复前思考被攒到正文出现才一次性吐出）。"""
    reasoning = "让我想想二分查找的原理。它依赖数组有序性，每次比较能排除一半。"
    r, _ = _collect(_char_stream("答案在这里。", reasoning=reasoning))
    assert r, "应产出 reasoning 分片"
    assert max(r) > 1, "思考不应逐字符下发: %s" % r
    assert sum(r) == len(reasoning), "思考内容不得丢失"


def test_reasoning_not_swallowed_by_content():
    """思考与正文都不丢，且正文到达前思考已 flush（渐进可见）。"""
    reasoning = "先分析问题。" * 4
    r, c = _collect(_char_stream("这是最终答案，长度足够触发合并阈值。", reasoning=reasoning))
    assert sum(r) == len(reasoning)
    assert sum(c) == len("这是最终答案，长度足够触发合并阈值。")


def test_real_tool_calls_still_pass_through_immediately():
    """真工具调用仍必须立即透传（不能被缓冲延迟）。"""
    async def gen():
        yield {"id": "c1", "created": 1, "model": "m",
               "choices": [{"index": 0, "delta": {"content": "查询中"},
                            "finish_reason": None}]}
        yield {"id": "c1", "created": 1, "model": "m",
               "choices": [{"index": 0, "delta": {"tool_calls": [
                   {"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"sh"}'}}]},
                   "finish_reason": None}]}

    async def _run():
        seen = []
        async for ck in _consolidate_openai_stream(gen(), False, 24):
            seen.append(ck)
        return seen

    out = asyncio.run(_run())
    tool_chunks = [c for c in out
                   if any((ch.get("delta") or {}).get("tool_calls")
                          for ch in (c.get("choices") or []))]
    assert tool_chunks, "工具调用必须透传"
    # 工具调用前缓冲的正文也应先 flush，不能丢
    text = "".join(
        (ch.get("delta") or {}).get("content") or ""
        for c in out for ch in (c.get("choices") or []))
    assert "查询中" in text


def test_drop_reasoning_discards():
    """reasoning=drop 时应彻底丢弃思考，只留正文。"""
    r, c = _collect(_char_stream("正文内容", reasoning="要被丢掉的思考"), drop_reasoning=True)
    assert r == [], "drop 模式不应有 reasoning 分片"
    assert sum(c) == len("正文内容")


def test_chunk_size_one_keeps_passthrough():
    """chunk_size=1 时保持原样直通（不因合并改变语义）。"""
    _, c = _collect(_char_stream("abcdef"), chunk_size=1)
    assert c == [1] * 6


def test_metadata_chunks_not_swallowed():
    """usage / finish_reason 收尾块必须仍在最后出现。"""
    async def gen():
        for ch in "答案":
            yield {"id": "c1", "created": 1, "model": "m",
                   "choices": [{"index": 0, "delta": {"content": ch}, "finish_reason": None}]}
        yield {"id": "c1", "created": 1, "model": "m",
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield {"id": "c1", "created": 1, "model": "m", "choices": [],
               "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    async def _run():
        out = []
        async for ck in _consolidate_openai_stream(gen(), False, 24):
            out.append(ck)
        return out

    out = asyncio.run(_run())
    assert out[-1].get("usage"), "独立 usage 块必须透传"
    assert any(ch.get("finish_reason") == "stop"
               for c in out for ch in (c.get("choices") or [])), "finish_reason 必须保留"
