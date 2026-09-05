"""
AIGate Responses API 兼容入口（给 Codex CLI 0.14x+ 用）。

Codex CLI 新版本只支持 wire_api="responses"（POST /v1/responses），
而 AIGate 核心只暴露 /v1/chat/completions。本 router 把
Responses API 请求翻译成内部 chat_completions 调用，再把响应
翻译回 Responses 格式（含流式 SSE），实现 codex → aigate 直连。

流程（与 anthropic_router 相同模式）：
  1. 鉴权（aigate_api_key）
  2. 解析 Responses 请求体
  3. 翻译为 ChatCompletionRequest
  4. 同进程调用 v1_router.chat_completions 核心
  5. 响应翻译回 Responses 格式
"""

import json
import time
import uuid
from typing import Optional, Any, AsyncIterator, Dict, List

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from server.schemas.chat import ChatCompletionRequest
from server.db import AsyncSessionLocal
from server.config import get_config


router = APIRouter(prefix="/v1", tags=["responses"])


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


def _verify_aigate_api_key(raw_request: Request):
    """与 anthropic_router 相同的 API key 鉴权。"""
    cfg = get_config()
    expected = getattr(cfg.security, "aigate_api_key", "") or ""
    if not expected:
        return
    token = raw_request.headers.get("x-api-key", "").strip()
    if not token:
        auth = raw_request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid AIGate API key")


# ---------------------------------------------------------------------------
# 请求翻译：Responses API -> Chat Completions
# ---------------------------------------------------------------------------

def _normalize_content_parts(parts: Any) -> Any:
    """Responses 内容部件 → OpenAI 部件（input_text/output_text → text；input_image → image_url）。

    无可识别部件时原样返回；全为纯文本时合并成字符串（兼容更多上游）。
    """
    if not isinstance(parts, list):
        return parts
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append({"type": "text", "text": p})
            continue
        if not isinstance(p, dict):
            continue
        ptype = p.get("type", "")
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            out.append({"type": "text", "text": p.get("text", "")})
        elif ptype == "input_image":
            url = p.get("image_url") or ""
            if not url and p.get("data"):
                url = f"data:{p.get('mime_type') or 'image/png'};base64,{p['data']}"
            if url:
                out.append({"type": "image_url", "image_url": {"url": url}})
    if not out:
        return parts
    if all(p["type"] == "text" for p in out):
        return "\n".join(p["text"] for p in out)
    return out


def _append_message(messages: List[dict], role: str, content, **extra) -> None:
    """追加消息；连续 assistant 消息合并（message + function_call 是同轮输出，拆开会被部分上游拒）。"""
    if role == "assistant" and messages and messages[-1].get("role") == "assistant":
        prev = messages[-1]
        prev_text = prev.get("content")
        cur_text = content if isinstance(content, str) else content
        if cur_text:
            if prev_text:
                prev["content"] = f"{prev_text}\n{cur_text}" if isinstance(prev_text, str) else prev_text
            else:
                prev["content"] = cur_text
        for k, v in extra.items():
            if k == "tool_calls":
                prev.setdefault("tool_calls", [])
                prev["tool_calls"].extend(v)
    else:
        m = {"role": role, "content": content}
        m.update(extra)
        messages.append(m)


def _input_to_messages(input_: Any, instructions: Optional[str]) -> List[dict]:
    """把 Responses 的 input（字符串 / 数组）翻译成 messages。"""
    messages: List[dict] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(input_, str):
        messages.append({"role": "user", "content": input_})
        return messages
    if not isinstance(input_, list):
        messages.append({"role": "user", "content": str(input_)})
        return messages
    for item in input_:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        role = item.get("role") or item.get("type")
        if role in ("user", "system", "assistant", "developer"):
            content = _normalize_content_parts(item.get("content"))
            _append_message(messages, role, content if content is not None else "")
        elif role == "message":
            inner = item.get("content")
            if isinstance(inner, list):
                _append_message(messages, item.get("role", "user"), _normalize_content_parts(inner))
            else:
                _append_message(messages, item.get("role", "user"), str(inner or ""))
        elif role == "function_call":
            # 历史 function_call item -> assistant message with tool_calls
            call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            name = item.get("name", "")
            args = item.get("arguments", "{}")
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)
            _append_message(
                messages, "assistant", None,
                tool_calls=[{"id": call_id, "type": "function", "function": {"name": name, "arguments": str(args)}}],
            )
        elif role == "function_call_output":
            call_id = item.get("call_id") or item.get("id") or ""
            output = item.get("output", "")
            if isinstance(output, (dict, list)):
                output = json.dumps(output, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": str(output)})
        elif role == "reasoning":
            # 推理摘要/加密块不参与 chat（跨上游无法解密复用）；有摘要文本时
            # 附到紧邻的 assistant 消息 reasoning_content，保住可见思维链
            summary = item.get("summary")
            text = ""
            if isinstance(summary, str):
                text = summary
            elif isinstance(summary, list):
                text = "\n".join(s.get("text", "") for s in summary if isinstance(s, dict))
            if text:
                _append_message(messages, "assistant", None, reasoning_content=text)
    return messages


def _tools_to_chat(tools: Optional[List[dict]]) -> Optional[List[dict]]:
    """Responses tools -> chat tools（格式基本一致）。"""
    if not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            fn = t.get("function", {})
            out.append({
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        elif t.get("type") == "code_interpreter" or t.get("type") == "web_search":
            # codex 主要用 function tools；其余忽略
            continue
        else:
            out.append(t)
    return out or None


def _responses_to_chat_request(body: dict) -> ChatCompletionRequest:
    model = body.get("model", "")
    stream = bool(body.get("stream", False))
    messages = _input_to_messages(body.get("input"), body.get("instructions"))
    tools = _tools_to_chat(body.get("tools"))
    req_dict = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "max_tokens": body.get("max_output_tokens"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
    }
    # 思考强度：Codex 的 reasoning.effort → reasoning_effort（透传给各 adapter 按方言翻译）
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        req_dict["reasoning_effort"] = reasoning["effort"]
    if tools:
        req_dict["tools"] = tools
    # codex 默认会让模型自行决定是否调用工具
    if body.get("tool_choice") is not None:
        tc = body["tool_choice"]
        if isinstance(tc, dict) and tc.get("type") == "function":
            req_dict["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
        else:
            req_dict["tool_choice"] = tc
    return ChatCompletionRequest(**{k: v for k, v in req_dict.items() if v is not None})


# ---------------------------------------------------------------------------
# 响应翻译：Chat Completions -> Responses API（非流式）
# ---------------------------------------------------------------------------

def _extract_body(openai_response) -> dict:
    if hasattr(openai_response, "body"):
        body = openai_response.body
        if isinstance(body, bytes):
            return json.loads(body.decode("utf-8", errors="replace"))
        return json.loads(str(body))
    if isinstance(openai_response, dict):
        return openai_response
    return {"data": str(openai_response)}


def _chat_to_responses(openai_dict: dict, model: str) -> dict:
    """把 OpenAI chat 响应翻译成 Responses API 响应。"""
    resp_id = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())
    choices = openai_dict.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content = message.get("content") or ""
    finish_reason = choice.get("finish_reason") or "stop"
    tool_calls = message.get("tool_calls") or []

    output_items: List[dict] = []
    if content:
        output_items.append({
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": content, "annotations": []}],
        })
    for tc in tool_calls:
        fn = tc.get("function", {})
        output_items.append({
            "id": f"fc_{uuid.uuid4().hex}",
            "type": "function_call",
            "status": "completed",
            "call_id": tc.get("id", f"call_{uuid.uuid4().hex[:12]}"),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "{}"),
        })
    if not output_items:
        output_items.append({
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        })

    usage = openai_dict.get("usage") or {}
    return {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": output_items,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": True,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": (usage.get("prompt_tokens") or 0),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": (usage.get("completion_tokens") or 0),
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": (usage.get("total_tokens") or 0),
        },
        "user": None,
        "metadata": {},
        "finish_reason": finish_reason,
    }


# ---------------------------------------------------------------------------
# 响应翻译：Chat SSE -> Responses SSE（流式）
# ---------------------------------------------------------------------------

def _chat_to_responses_stream(openai_stream: StreamingResponse, model: str) -> StreamingResponse:
    resp_id = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())
    _iterator = openai_stream.body_iterator

    def _sse(event: dict) -> bytes:
        return f"event: {event.get('type', 'response.output_text.delta')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")

    async def responses_stream() -> AsyncIterator[bytes]:
        # 开头发 response.created 事件
        yield _sse({
            "type": "response.created",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": created,
                "status": "in_progress",
                "output": [],
            },
        })
        tool_state: Dict[str, dict] = {}
        output_items: List[dict] = []
        text_buffer = ""
        text_item_id = None
        text_started = False
        finish_reason = "stop"
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        try:
            async for chunk_bytes in _iterator:
                if isinstance(chunk_bytes, bytes):
                    chunk_str = chunk_bytes.decode("utf-8", errors="replace")
                else:
                    chunk_str = str(chunk_bytes)
                for line in chunk_str.split("\n"):
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue
                    # ── 终态错误：网关全部候选失败时发 {"error": ...} chunk（不带 choices），
                    #    必须翻译为 response.failed，而不是继续走正常 completed 流程伪装成功 ──
                    if isinstance(chunk, dict) and chunk.get("error") and not chunk.get("choices"):
                        _err = chunk.get("error")
                        _msg = _err.get("message", "") if isinstance(_err, dict) else str(_err)
                        yield _sse({
                            "type": "response.failed",
                            "response": {
                                "id": resp_id,
                                "object": "response",
                                "created_at": created,
                                "status": "failed",
                                "error": {"code": "upstream_error", "message": _msg[:500]},
                                "output": [],
                            },
                        })
                        return
                    # usage
                    u = chunk.get("usage") or {}
                    if u:
                        usage["prompt_tokens"] = u.get("prompt_tokens") or usage["prompt_tokens"]
                        usage["completion_tokens"] = u.get("completion_tokens") or usage["completion_tokens"]
                        usage["total_tokens"] = u.get("total_tokens") or usage["total_tokens"]
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    if delta.get("content"):
                        if not text_started:
                            # 首个文本块：先发送 output_item.added / content_part.added
                            text_item_id = f"msg_{uuid.uuid4().hex}"
                            text_started = True
                            yield _sse({
                                "type": "response.output_item.added",
                                "output_index": 0,
                                "item": {
                                    "id": text_item_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "status": "in_progress",
                                    "content": [],
                                },
                            })
                            yield _sse({
                                "type": "response.content_part.added",
                                "item_id": text_item_id,
                                "output_index": 0,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": "", "annotations": []},
                            })
                        text_buffer += delta["content"]
                        yield _sse({
                            "type": "response.output_text.delta",
                            "item_id": text_item_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": delta["content"],
                        })
                    # 工具调用（流式逐步累积）
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        st = tool_state.setdefault(idx, {
                            "item_id": f"fc_{uuid.uuid4().hex}",
                            "call_id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                            "name": "",
                            "arguments": "",
                            "started": False,
                        })
                        if tc.get("id"):
                            st["call_id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if not st["started"] and (fn.get("name") or fn.get("arguments")):
                            st["started"] = True
                            yield _sse({
                                "type": "response.output_item.added",
                                "output_index": 0,
                                "item": {
                                    "id": st["item_id"],
                                    "type": "function_call",
                                    "status": "in_progress",
                                    "call_id": st["call_id"],
                                    "name": fn.get("name", ""),
                                    "arguments": "",
                                },
                            })
                        if fn.get("name"):
                            st["name"] = fn["name"]
                        if fn.get("arguments"):
                            st["arguments"] += fn["arguments"]
                            yield _sse({
                                "type": "response.function_call_arguments.delta",
                                "item_id": st["item_id"],
                                "output_index": 0,
                                "delta": fn.get("arguments", ""),
                            })
                    fr = (chunk.get("choices") or [{}])[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
            # 收尾：发送 done 事件
            if text_started and text_item_id:
                yield _sse({
                    "type": "response.output_text.done",
                    "item_id": text_item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "text": text_buffer,
                })
                yield _sse({
                    "type": "response.content_part.done",
                    "item_id": text_item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": text_buffer, "annotations": []},
                })
                yield _sse({
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": text_item_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text_buffer, "annotations": []}],
                    },
                })
            # function_call done 事件
            for idx, st in tool_state.items():
                if st["started"]:
                    yield _sse({
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": st["item_id"],
                            "type": "function_call",
                            "status": "completed",
                            "call_id": st["call_id"],
                            "name": st["name"],
                            "arguments": st["arguments"],
                        },
                    })
            # 结束：组装 output 并发送 response.completed
            if text_buffer and text_item_id:
                output_items.append({
                    "id": text_item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text_buffer, "annotations": []}],
                })
            for idx, st in tool_state.items():
                output_items.append({
                    "id": st["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "call_id": st["call_id"],
                    "name": st["name"],
                    "arguments": st["arguments"],
                })
            yield _sse({
                "type": "response.completed",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "created_at": created,
                    "status": "completed",
                    "error": None,
                    "incomplete_details": None,
                    "instructions": None,
                    "max_output_tokens": None,
                    "model": model,
                    "output": output_items,
                    "parallel_tool_calls": True,
                    "previous_response_id": None,
                    "reasoning": {"effort": None, "summary": None},
                    "store": True,
                    "temperature": None,
                    "text": {"format": {"type": "text"}},
                    "tool_choice": "auto",
                    "tools": [],
                    "top_p": None,
                    "truncation": "disabled",
                    "usage": {
                        "input_tokens": usage["prompt_tokens"],
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": usage["completion_tokens"],
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": usage["total_tokens"],
                    },
                    "user": None,
                    "metadata": {},
                    "finish_reason": finish_reason,
                },
            })
        except Exception as e:
            yield _sse({
                "type": "response.failed",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "status": "failed",
                    "error": {"code": "api_error", "message": str(e)[:500]},
                    "output": [],
                },
            })

    return StreamingResponse(responses_stream(), media_type="text/event-stream")


def _error_response(error: dict, status_code: int) -> JSONResponse:
    return JSONResponse(content={"type": "error", "error": error, "response": None}, status_code=status_code)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

@router.post("/responses")
async def responses_completions(
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        _verify_aigate_api_key(raw_request)
    except HTTPException as auth_err:
        raise auth_err

    try:
        body = await raw_request.json()
    except Exception:
        return _error_response({"code": "invalid_request_error", "message": "invalid JSON body"}, 400)

    # 翻译请求
    try:
        openai_req = _responses_to_chat_request(body)
    except Exception as e:
        return _error_response({"code": "invalid_request_error", "message": f"request conversion failed: {e}"}, 400)

    # 调用核心
    try:
        from server.api.v1_router import chat_completions
        openai_response = await chat_completions(openai_req, raw_request, db)
    except Exception as e:
        return _error_response({"code": "api_error", "message": str(e)[:500]}, 503)

    # 响应翻译
    try:
        if isinstance(openai_response, StreamingResponse):
            return _chat_to_responses_stream(openai_response, openai_req.model)
        body_dict = _extract_body(openai_response)
        status = getattr(openai_response, "status_code", 200)
        if status != 200 or "error" in body_dict:
            err = body_dict.get("error", "upstream error")
            if isinstance(err, dict):
                err = err.get("message", str(err))
            return _error_response({"code": "api_error", "message": str(err)}, status or 502)
        return JSONResponse(content=_chat_to_responses(body_dict, openai_req.model), status_code=200)
    except Exception as e:
        return _error_response({"code": "api_error", "message": str(e)[:500]}, 502)
