"""
Anthropic ↔ OpenAI 格式双向转换器
负责把 Anthropic Messages API 格式翻译成内部 OpenAI Chat Completions 格式，
再把上游 OpenAI 响应翻译回 Anthropic 格式返回给客户端。

支持：
- 非流式文本对话
- 流式文本对话（SSE 事件流）
- system prompt（单独字段 → messages[0]）
- temperature / top_p / max_tokens / stop_sequences
- usage 映射（prompt_tokens ↔ input_tokens, completion_tokens ↔ output_tokens）
- stop_reason 映射（stop → end_turn, length → max_tokens, tool_calls → tool_use）
"""
import json
import time
import uuid
import asyncio
from typing import Optional, Dict, Any, List


def anthropic_to_openai_request(req: dict) -> dict:
    """Anthropic Messages 请求 → OpenAI Chat Completions 请求

    Anthropic 格式:
      model, system, messages[{role, content}], max_tokens, temperature, ...
    OpenAI 格式:
      model, messages[{role, content}], max_tokens, temperature, ...
    """
    messages: List[dict] = []

    # system prompt → messages[0] {"role": "system", "content": ...}
    system = req.get("system")
    if system:
        if isinstance(system, list):
            # list of blocks，拼接 text
            parts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            system_text = "\n".join(parts)
        else:
            system_text = str(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    # Anthropic messages → OpenAI messages
    for msg in req.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content")
        # content 可以是 string 或 list of blocks
        if isinstance(content, list):
            blocks = []
            tool_calls = []
            for block in (content or []):
                btype = block.get("type", "")
                if btype == "text":
                    blocks.append(block.get("text", ""))
                elif btype == "tool_use":
                    # assistant tool_use → OpenAI tool_calls
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        }
                    })
                elif btype == "tool_result":
                    # user tool_result → OpenAI tool message
                    tc_content = block.get("content", "")
                    if isinstance(tc_content, list):
                        # 拼接 text blocks
                        parts = []
                        for tb in tc_content:
                            if isinstance(tb, dict) and tb.get("type") == "text":
                                parts.append(tb.get("text", ""))
                            elif isinstance(tb, str):
                                parts.append(tb)
                        tc_content = "\n".join(parts)
                    messages.append({
                        "role": role,
                        "content": str(tc_content),
                        "tool_call_id": block.get("tool_use_id", ""),
                    })
                    continue
            if tool_calls:
                messages.append({"role": role, "content": "\n".join(blocks) or None, "tool_calls": tool_calls})
            elif blocks:
                messages.append({"role": role, "content": "\n".join(blocks)})
            elif not blocks and not tool_calls:
                messages.append({"role": role, "content": ""})
        else:
            messages.append({"role": role, "content": content})

    openai_req: dict = {
        "model": req.get("model", "auto"),
        "messages": messages,
        "max_tokens": req.get("max_tokens", 4096),
    }
    # 可选参数透传
    if req.get("temperature") is not None:
        openai_req["temperature"] = req["temperature"]
    if req.get("top_p") is not None:
        openai_req["top_p"] = req["top_p"]
    if req.get("stop_sequences"):
        openai_req["stop"] = req["stop_sequences"]
    if req.get("stream"):
        openai_req["stream"] = True
    if req.get("tools"):
        openai_req["tools"] = _anthropic_tools_to_openai(req["tools"])
    if req.get("tool_choice"):
        openai_req["tool_choice"] = _anthropic_tool_choice_to_openai(req["tool_choice"])
    return openai_req


def _anthropic_tools_to_openai(tools: list) -> list:
    """Anthropic tools 格式 → OpenAI tools 格式

    Anthropic: {"name": "...", "description": "...", "input_schema": {...}}
    OpenAI:     {"type": "function", "function": {"name", "description", "parameters"}}
    """
    result = []
    for t in (tools or []):
        if not isinstance(t, dict):
            continue
        result.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            }
        })
    return result


def _anthropic_tool_choice_to_openai(tc: Any) -> Any:
    if isinstance(tc, dict):
        tc_type = tc.get("type")
        if tc_type == "auto":
            return "auto"
        if tc_type == "any":
            return "required"
        if tc_type == "tool":
            return {"type": "function", "function": {"name": tc.get("name", "")}}
    return "auto"


def openai_response_to_anthropic(openai_resp: dict, model_name: str) -> dict:
    """OpenAI 非流式响应 → Anthropic Messages 响应"""
    choices = openai_resp.get("choices", [])
    choice = choices[0] if choices else {}
    openai_msg = choice.get("message", {})
    text = openai_msg.get("content") or ""
    content_blocks = []
    # 检查 tool_calls
    tool_calls = openai_msg.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except Exception:
            args = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": args,
        })
    if text:
        content_blocks.insert(0, {"type": "text", "text": text})

    finish_reason = choice.get("finish_reason", "stop")
    stop_reason = _openai_finish_to_anthropic_stop(finish_reason)

    usage = openai_resp.get("usage", {})
    return {
        "id": f"msg_{openai_resp.get('id', uuid.uuid4().hex)}",
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": content_blocks or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
        }
    }


def _openai_finish_to_anthropic_stop(finish: str) -> str:
    """OpenAI finish_reason → Anthropic stop_reason"""
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "end_turn",
    }
    return mapping.get(finish, "end_turn")


def openai_error_to_anthropic(error: dict, status_code: int = 500) -> dict:
    """OpenAI 错误 → Anthropic 错误格式"""
    err_type = "api_error"
    msg = "Internal error"
    if isinstance(error, dict):
        msg = str(error.get("error", error))
        if status_code == 401:
            err_type = "authentication_error"
        elif status_code == 400:
            err_type = "invalid_request_error"
        elif status_code == 429:
            err_type = "rate_limit_error"
        elif 500 <= status_code < 600:
            err_type = "api_error"
        elif status_code == 404:
            err_type = "not_found_error"
    return {
        "type": "error",
        "error": {
            "type": err_type,
            "message": msg,
        }
    }


# ─── 流式转换：OpenAI SSE chunks → Anthropic SSE 事件 ───

async def openai_stream_to_anthropic_events(openai_chunk: dict, state: dict) -> List[dict]:
    """把一个 OpenAI SSE chunk 转换成 Anthropic SSE 事件列表（可能 0~多个）

    state 维护跨 chunk 状态：
      - msg_id: Anthropic message id
      - model: 目标模型名
      - started: 是否已发 message_start
      - block_started: 是否已发 content_block_start
      - text_so_far: 累计文本
      - finish_reason: finish_reason
      - usage_in/usage_out: token 统计
    """
    events: List[dict] = []

    if not state.get("started"):
        # 第一个 chunk: 发 message_start
        events.append({
            "type": "message_start",
            "message": {
                "id": state.get("msg_id", f"msg_{uuid.uuid4().hex}"),
                "type": "message",
                "role": "assistant",
                "model": state.get("model", ""),
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": state.get("usage_in", 0),
                    "output_tokens": 0,
                }
            }
        })
        # 发 content_block_start（第一个 text block）
        events.append({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""}
        })
        state["started"] = True
        state["block_started"] = True

    # 解析 OpenAI chunk
    choices = openai_chunk.get("choices", [])
    if choices:
        choice = choices[0]
        delta = choice.get("delta", {})
        text = delta.get("content")
        if text:
            # 发 content_block_delta
            events.append({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text}
            })
        finish = choice.get("finish_reason")
        if finish:
            state["finish_reason"] = finish

    # usage 统计（部分提供商在最后一个 chunk 带 usage）
    u = openai_chunk.get("usage")
    if u:
        state["usage_in"] = int(u.get("prompt_tokens", 0))
        state["usage_out"] = int(u.get("completion_tokens", 0))

    return events


def openai_stream_end_events(state: dict) -> List[dict]:
    """OpenAI 流结束 [DONE] → 发送 Anthropic message_stop 等收尾事件"""
    events: List[dict] = []
    if state.get("block_started"):
        events.append({"type": "content_block_stop", "index": 0})
    stop_reason = _openai_finish_to_anthropic_stop(state.get("finish_reason", "stop"))
    events.append({
        "type": "message_delta",
        "delta": {
            "stop_reason": stop_reason,
            "stop_sequence": None,
        },
        "usage": {
            "output_tokens": state.get("usage_out", 0),
        }
    })
    events.append({"type": "message_stop"})
    return events


def format_anthropic_sse(event: dict) -> bytes:
    """把 Anthropic 事件格式化成 SSE 二行格式"""
    event_type = event.get("type", "")
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")
