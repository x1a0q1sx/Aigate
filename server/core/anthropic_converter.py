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
      model, system, messages[{role, content}], max_tokens, temperature, thinking, ...
    OpenAI 格式:
      model, messages[{role, content}], max_tokens, temperature, reasoning, reasoning_effort, ...
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
            images = []
            tool_calls = []
            reasoning_text = ""
            for block in (content or []):
                btype = block.get("type", "")
                if btype == "text":
                    blocks.append(block.get("text", ""))
                elif btype == "thinking":
                    # 历史思考块 → reasoning_content（Claude Code 多轮会回传）
                    t = block.get("thinking", "")
                    if isinstance(t, str) and t:
                        reasoning_text = t
                elif btype == "image":
                    # 图片块 → OpenAI image_url 部件（base64 data URL 直传）
                    src = block.get("source") or {}
                    if isinstance(src, dict) and src.get("data"):
                        media = src.get("media_type") or "image/png"
                        images.append(f"data:{media};base64,{src['data']}")
                    elif isinstance(src, dict) and src.get("url"):
                        images.append(src["url"])
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
                    img_urls = []
                    if isinstance(tc_content, list):
                        # 拼接 text blocks；图片转 data URL 文本标记
                        parts = []
                        for tb in tc_content:
                            if isinstance(tb, dict) and tb.get("type") == "text":
                                parts.append(tb.get("text", ""))
                            elif isinstance(tb, dict) and tb.get("type") == "image":
                                src = tb.get("source") or {}
                                if isinstance(src, dict) and src.get("data"):
                                    media = src.get("media_type") or "image/png"
                                    img_urls.append(f"data:{media};base64,{src['data']}")
                            elif isinstance(tb, str):
                                parts.append(tb)
                        tc_content = "\n".join(parts)
                        if img_urls:
                            tc_content = (str(tc_content) + "\n" if tc_content else "") + "\n".join(
                                f"[image attached: {u[:64]}...]" for u in img_urls)
                    messages.append({
                        "role": role,
                        "content": str(tc_content),
                        "tool_call_id": block.get("tool_use_id", ""),
                    })
                    continue
            if tool_calls:
                m: dict = {"role": role, "content": "\n".join(blocks) or None, "tool_calls": tool_calls}
                if reasoning_text:
                    m["reasoning_content"] = reasoning_text
                messages.append(m)
            elif images:
                # 混合文本+图片 → OpenAI 多模态 content 数组
                parts = [{"type": "text", "text": "\n".join(blocks)}] if blocks else []
                parts += [{"type": "image_url", "image_url": {"url": u}} for u in images]
                m = {"role": role, "content": parts}
                if reasoning_text:
                    m["reasoning_content"] = reasoning_text
                messages.append(m)
            elif blocks:
                m = {"role": role, "content": "\n".join(blocks)}
                if reasoning_text:
                    m["reasoning_content"] = reasoning_text
                messages.append(m)
            elif reasoning_text:
                messages.append({"role": role, "content": "", "reasoning_content": reasoning_text})
            else:
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
    # thinking 参数：Anthropic thinking → OpenAI reasoning（anthropic 出站适配器原生识别）
    # 同时反推 effort 档位，让 openai_compat / codex_responses 上游也能控制思考深度
    thinking = req.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        openai_req["reasoning"] = {
            "type": "enabled",
            "budget_tokens": int(thinking.get("budget_tokens") or 0),
        }
        openai_req["reasoning_effort"] = _budget_to_effort(int(thinking.get("budget_tokens") or 0))
    return openai_req


def _budget_to_effort(budget_tokens: int) -> str:
    """Anthropic 思考预算 → OpenAI effort 档位（近似反推）。"""
    if budget_tokens <= 0:
        return "low"
    if budget_tokens <= 2048:
        return "low"
    if budget_tokens <= 8192:
        return "medium"
    if budget_tokens <= 16384:
        return "high"
    return "xhigh"


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
    reasoning = openai_msg.get("reasoning_content")
    if not isinstance(reasoning, str):
        reasoning = None
    content_blocks = []
    if reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
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
      - blocks: [{type: text|thinking|tool_use, ...}] 已开启的内容块
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
        state["started"] = True
        state.setdefault("blocks", [])
        state.setdefault("next_index", 0)

    blocks_state: list = state.setdefault("blocks", [])
    state.setdefault("next_index", 0)

    def _open_block(btype: str, extra: dict) -> int:
        idx = state["next_index"]
        state["next_index"] = idx + 1
        block = {"type": btype}
        block.update(extra)
        blocks_state.append({"type": btype, "index": idx})
        events.append({"type": "content_block_start", "index": idx, "content_block": block})
        return idx

    def _close_block(btype: str):
        for b in blocks_state:
            if b["type"] == btype and not b.get("closed"):
                b["closed"] = True
                events.append({"type": "content_block_stop", "index": b["index"]})
                return

    # 解析 OpenAI chunk
    choices = openai_chunk.get("choices", [])
    if choices:
        choice = choices[0]
        delta = choice.get("delta", {})
        text = delta.get("content")
        reasoning = delta.get("reasoning_content")
        if not isinstance(reasoning, str):
            r2 = delta.get("reasoning")
            reasoning = r2 if isinstance(r2, str) else None
        if reasoning:
            # 思考增量 → thinking 块（与文本块分离，Claude Code 前端可折叠展示）
            idx = None
            for b in blocks_state:
                if b["type"] == "thinking" and not b.get("closed"):
                    idx = b["index"]
                    break
            if idx is None:
                if any(b["type"] == "text" for b in blocks_state):
                    _close_block("text")
                idx = _open_block("thinking", {"thinking": "", "signature": ""})
            events.append({
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "thinking_delta", "thinking": reasoning},
            })
        if text:
            # 思考结束才能开始正文（thinking 块必须在 text 之前关闭）
            _close_block("thinking")
            idx = None
            for b in blocks_state:
                if b["type"] == "text" and not b.get("closed"):
                    idx = b["index"]
                    break
            if idx is None:
                idx = _open_block("text", {"text": ""})
            events.append({
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "text_delta", "text": text},
            })
        # 工具调用增量 → tool_use 块（流式 input_json_delta）
        for tc in (delta.get("tool_calls") or []):
            tc_idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            target = None
            for b in blocks_state:
                if b["type"] == "tool_use" and b.get("tc_index") == tc_idx and not b.get("closed"):
                    target = b
                    break
            if target is None:
                # 新工具调用：先关掉前面的文本/思考块
                _close_block("thinking")
                _close_block("text")
                idx = _open_block("tool_use", {
                    "id": str(tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"),
                    "name": fn.get("name") or "",
                    "input": {},
                })
                target = {"type": "tool_use", "index": idx, "tc_index": tc_idx}
                blocks_state.append(target)
                state["has_tool_use"] = True
            args_delta = fn.get("arguments")
            if args_delta:
                events.append({
                    "type": "content_block_delta",
                    "index": target["index"],
                    "delta": {"type": "input_json_delta", "partial_json": args_delta},
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
    for b in state.get("blocks", []):
        if not b.get("closed"):
            events.append({"type": "content_block_stop", "index": b["index"]})
    # 兼容旧 state（无 blocks 字段）
    if not state.get("blocks") and state.get("block_started"):
        events.append({"type": "content_block_stop", "index": 0})
    finish = state.get("finish_reason", "stop")
    stop_reason = _openai_finish_to_anthropic_stop(finish)
    if state.get("has_tool_use") and finish not in ("stop", None):
        stop_reason = "tool_use"
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
