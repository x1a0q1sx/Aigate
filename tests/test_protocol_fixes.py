"""P2-9 协议契约 fixture 测试：三个客户端协议面的完整 SSE 事件序列回归。

目的：把 Codex（/v1/responses）、Claude（/v1/messages）转换层对同一份
上游 OpenAI chat SSE 的**完整事件序列**固定成样例，防止后续路由/兼容
优化破坏客户端契约（此前终态错误伪装成功、tool_calls 丢失等回归均是
单事件粒度测试抓不到的）。

fixture 是手工构造的真实感上游流（含 reasoning / 正文 / 工具调用 / usage）。
"""
import json
import pytest
from fastapi.responses import StreamingResponse

from server.schemas.chat import ChatCompletionRequest


# ── 上游 OpenAI chat SSE fixture（一次带思考+正文+工具调用+usage 的完整流） ──

UPSTREAM_STREAM = b"""data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}

data: {"choices":[{"index":0,"delta":{"reasoning_content":"let me think"}}]}

data: {"choices":[{"index":0,"delta":{"content":"Hel"}}]}

data: {"choices":[{"index":0,"delta":{"content":"lo world"}}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"get_weather","arguments":"{\\"city\\":"}}]}}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"SF\\"}"}}]}}]}

data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}}

data: [DONE]

"""


async def _collect(gen) -> list:
    out = []
    async for piece in gen:
        out.append(piece if isinstance(piece, bytes) else str(piece).encode())
    return out


def _parse_sse_events(chunks: list) -> list:
    """把 SSE 字节流解析为 (event_type, data_dict) 列表（Responses 风格：event 行为 type 值）。"""
    events = []
    buf = b"".join(chunks).decode("utf-8", "replace")
    for block in buf.split("\n\n"):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        etype, data = None, None
        for l in lines:
            if l.startswith("event:"):
                etype = l[6:].strip()
            elif l.startswith("data:"):
                data = l[5:].strip()
        if data:
            try:
                d = json.loads(data)
            except Exception:
                continue
            events.append((etype or d.get("type", ""), d))
    return events


# ── Codex /v1/responses：上游 chat SSE → Responses SSE 契约 ──

@pytest.mark.asyncio
async def test_responses_stream_full_contract():
    from server.api.responses_router import _chat_to_responses_stream

    async def gen():
        yield UPSTREAM_STREAM
    sr = _chat_to_responses_stream(StreamingResponse(gen(), media_type="text/event-stream"), "combo:x")
    events = _parse_sse_events([c async for c in sr.body_iterator])
    types = [t for t, _ in events]

    # 事件序列骨架：created 开头、completed 收尾、顺序完整
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert "response.output_text.delta" in types
    assert "response.output_item.added" in types
    assert "response.function_call_arguments.delta" in types
    assert types.index("response.output_item.added") < types.index("response.output_text.delta")

    # 正文按序拼接
    text = "".join(d.get("delta", "") for t, d in events if t == "response.output_text.delta")
    assert text == "Hello world"

    # 工具调用参数完整
    args = "".join(d.get("delta", "") for t, d in events if t == "response.function_call_arguments.delta")
    assert json.loads(args) == {"city": "SF"}

    # completed 事件：output 含 message + function_call，usage 透传
    completed = events[-1][1]["response"]
    assert completed["status"] == "completed"
    kinds = [o["type"] for o in completed["output"]]
    assert "message" in kinds and "function_call" in kinds
    fc = next(o for o in completed["output"] if o["type"] == "function_call")
    assert fc["name"] == "get_weather" and fc["call_id"] == "call_1"
    assert completed["usage"]["input_tokens"] == 100
    assert completed["usage"]["total_tokens"] == 120


# ── Claude /v1/messages：上游 chat SSE → Anthropic SSE 契约 ──

@pytest.mark.asyncio
async def test_anthropic_stream_full_contract():
    from server.api.anthropic_router import _convert_streaming_response

    async def gen():
        yield UPSTREAM_STREAM
    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    sr = _convert_streaming_response(StreamingResponse(gen(), media_type="text/event-stream"), req)
    events = _parse_sse_events([c async for c in sr.body_iterator])
    types = [t for t, _ in events]

    # 事件序列骨架：message_start → 内容块 → message_delta → message_stop
    # （keepalive 为 SSE 注释行，非事件，不进 types）
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    assert "message_delta" in types
    # 块序列：先 thinking 块再 text 块再 tool_use 块
    starts = [(d["content_block"]["type"], d["index"]) for t, d in events if t == "content_block_start"]
    assert starts[0][0] == "thinking"
    assert "text" in [s[0] for s in starts] and "tool_use" in [s[0] for s in starts]

    # 思考增量在文本增量之前（块序约束）
    first_think = next(i for i, t in enumerate(types) if t == "content_block_delta"
                       and events[i][1]["delta"]["type"] == "thinking_delta")
    first_text = next(i for i, t in enumerate(types) if t == "content_block_delta"
                      and events[i][1]["delta"]["type"] == "text_delta")
    assert first_think < first_text

    # 工具调用参数经 input_json_delta 完整送达
    args = "".join(d["delta"]["partial_json"] for t, d in events
                   if t == "content_block_delta" and d["delta"]["type"] == "input_json_delta")
    assert json.loads(args) == {"city": "SF"}

    # 收尾：stop_reason=tool_use、usage 透传
    delta = next(d for t, d in events if t == "message_delta")
    assert delta["delta"]["stop_reason"] == "tool_use"
    assert delta["usage"]["output_tokens"] == 20
    start_msg = next(d for t, d in events if t == "message_start")
    assert start_msg["message"]["model"] == "m"


# ── Claude 非流式：OpenAI 非流式响应 → Anthropic message 契约 ──

def test_anthropic_nonstream_contract():
    from server.core.anthropic_converter import openai_response_to_anthropic

    openai_resp = {
        "id": "chatcmpl-x", "choices": [{
            "index": 0, "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": "Let me check.",
                "reasoning_content": "internal thought",
                "tool_calls": [{"id": "call_9", "type": "function",
                                "function": {"name": "get_weather", "arguments": "{\"city\":\"SF\"}"}}],
            },
        }],
        "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
    }
    d = openai_response_to_anthropic(openai_resp, "claude-x")
    assert d["type"] == "message" and d["role"] == "assistant"
    assert d["stop_reason"] == "tool_use"
    kinds = [b["type"] for b in d["content"]]
    assert kinds == ["thinking", "text", "tool_use"]  # 思考在前，正文与工具随后
    assert d["content"][1]["text"] == "Let me check."
    assert d["content"][2]["input"] == {"city": "SF"}
    assert d["usage"] == {"input_tokens": 50, "output_tokens": 30}
