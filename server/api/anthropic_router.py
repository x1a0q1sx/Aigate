"""
Anthropic Messages API 兼容路由
POST /v1/messages → 接收 Anthropic 格式 → 内部转 OpenAI → 调 AIGate 路由 → 转回 Anthropic

支持：
- system prompt
- temperature / top_p / max_tokens / stop_sequences
- 非流式文本对话
- 流式文本对话（SSE 事件）
- usage 映射
暂不完整支持（第一版先忽略差异）：
- tools / tool_use（结构差异大）
- vision / image
"""
import json
import time
import uuid
from typing import Optional, Any
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from server.db import AsyncSessionLocal
from server.core.anthropic_converter import (
    anthropic_to_openai_request,
    openai_response_to_anthropic,
    openai_error_to_anthropic,
    openai_stream_to_anthropic_events,
    openai_stream_end_events,
    format_anthropic_sse,
)
from server.schemas.chat import ChatCompletionRequest
from server.config import get_config

config = get_config()
router = APIRouter()


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


def _verify_aigate_api_key(raw_request: Request):
    """鉴权：AIGate 自身 API Key。

    Anthropic SDK 用 x-api-key 头，但 Claude Code 也会发 Authorization。
    这里两种都兼容。
    """
    expected = getattr(config.security, "aigate_api_key", "") or ""
    if not expected:
        return
    # 1. x-api-key
    token = raw_request.headers.get("x-api-key", "").strip()
    if not token:
        # 2. Authorization: Bearer xxx
        auth = raw_request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid AIGate API key")


def _extract_anthropic_error_message(e: Exception) -> str:
    """从上游 OpenAI 异常里提取简要错误说明"""
    s = str(e)
    if "\nResponse: " in s:
        return s.split("\nResponse: ", 1)[1][:500]
    return s[:500]


@router.post("/v1/messages")
async def anthropic_messages(
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Anthropic Messages API 兼容入口"""
    # 1. 鉴权
    try:
        _verify_aigate_api_key(raw_request)
    except HTTPException as auth_err:
        raise auth_err

    # 2. 解析 Anthropic 请求体
    try:
        anthropic_body = await raw_request.json()
    except Exception:
        return _response_error({"error": "invalid JSON body"}, 400)

    # 3. 翻译成 OpenAI Chat Completions 格式
    try:
        openai_req_dict = anthropic_to_openai_request(anthropic_body)
    except Exception as e:
        return _response_error({"error": "request conversion failed: " + str(e)}, 400)

    # 4. 构造 ChatCompletionRequest 对象传给现有路由核心
    try:
        openai_req = ChatCompletionRequest(**openai_req_dict)
    except Exception as e:
        return _response_error({"error": "invalid converted OpenAI request: " + str(e)}, 400)

    # 5. 调用现有 /v1/chat/completions 核心逻辑
    #    为避免代码重复，我们内部转发 HTTP 请求到 uvicorn 自身
    #    但更高效的做法是直接 import chat_completions 函数体，
    #    这里为了不耦合，用内部 HTTP 调用。
    #    两种选择：
    #    A) 尊重边界，发内部 HTTP → 简单但有 TCP 往返开销
    #    B) 抽取 chat_completions 核心为独立函数，两处复用
    #    我选 B 的轻量版：直接调用核心函数（同进程同事件循环）

    # 6. 核心调用：把请求丢给现有 chat_completions
    try:
        result = await _call_internal_chat_completions(openai_req, raw_request, db)
    except httpx.HTTPStatusError as e:
        return _response_error({"error": _extract_anthropic_error_message(e)}, e.response.status_code if hasattr(e.response, "status_code", ) else 502)
    except Exception as e:
        return _response_error({"error": str(e)[:500]}, 503)

    # 7. 翻译响应
    return result


async def _call_internal_chat_completions(openai_req: ChatCompletionRequest, raw_request: Request, db: AsyncSession):
    """直接调用现有 v1_router.chat_completions 核心"""
    from server.api.v1_router import chat_completions

    # 当前 chat_completions 期望通过 FastAPI 依赖注入的方式调用，
    # 但同进程内直接调用 async 函数即可，注入 db 由我们传。
    openai_response = await chat_completions(openai_req, raw_request, db)

    # OpenAI 路由返回值可能是 JSONResponse / 文件字典 / StreamingResponse
    # 我们要在这层把响应翻译回 Anthropic 格式
    return await _translate_openai_response_to_anthropic(openai_response, openai_req, anthropic_model=raw_request, db=db)


async def _translate_openai_response_to_anthropic(openai_response, openai_req, anthropic_model, db):
    """把 OpenAI 路由的响应翻译回 Anthropic 格式"""
    # 可能的响应类型：
    # - StreamingResponse（流式）
    # - JSONResponse / dict（非流式）
    # - 带错误码的 JSONResponse

    if isinstance(openai_response, StreamingResponse):
        return _convert_streaming_response(openai_response, openai_req)

    # 非流式：拿 body
    if hasattr(openai_response, "body"):
        # JSONResponse 有 body attribute
        body_bytes = openai_response.body
        if isinstance(body_bytes, bytes):
            body_str = body_bytes.decode("utf-8")
        else:
            body_str = str(body_bytes)
        try:
            openai_dict = json.loads(body_str)
        except Exception:
            openai_dict = {"error": body_str}
    elif isinstance(openai_response, dict):
        openai_dict = openai_response
    else:
        openai_dict = {"data": str(openai_response)}

    # 检查错误
    status_code = getattr(openai_response, "status_code", 200)
    if status_code != 200 or "error" in openai_dict:
        err_msg = openai_dict.get("error", "upstream error")
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("message", str(err_msg))
        return _response_error({"error": str(err_msg)}, status_code or 502)

    # 正常响应 → Anthropic 格式
    # 上游路由会设置 model 字段为 "<provider>/<model_id>" 这种全名
    model_name = openai_dict.get("model", openai_req.model or "unknown")
    if model_name == "auto":
        # auto 通常会被路由替换成真实模型
        model_name = openai_dict.get("model", "auto")
    anthropic_resp = openai_response_to_anthropic(openai_dict, model_name)
    return JSONResponse(content=anthropic_resp, status_code=200)


def _convert_streaming_response(openai_response: StreamingResponse, openai_req: ChatCompletionRequest):
    """把 OpenAI SSE 流转换成 Anthropic SSE 事件流"""
    msg_id = f"msg_{uuid.uuid4().hex}"
    state = {
        "started": False,
        "block_started": False,
        "msg_id": msg_id,
        "model": openai_req.model or "",
        "finish_reason": None,
        "usage_in": 0,
        "usage_out": 0,
    }

    async def anthropic_stream():
        try:
            # keepalive：SSE 注释行（非事件），防上游思考期客户端超时
            yield b": keepalive\n\n"
            async for chunk_bytes in openai_response.body_iterator:
                if isinstance(chunk_bytes, bytes):
                    chunk_str = chunk_bytes.decode("utf-8", errors="replace")
                else:
                    chunk_str = str(chunk_bytes)
                # 解析 SSE: "data: {...}\n\n"
                for line in chunk_str.split("\n"):
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        for ev in openai_stream_end_events(state):
                            yield format_anthropic_sse(ev)
                        return
                    try:
                        openai_chunk = json.loads(data_str)
                    except Exception:
                        continue
                    # ── 终态错误：网关全部候选失败时发 {"error": ...} chunk（不带 choices），
                    #    必须翻译为 Anthropic error 事件终止，而不是伪装成正常 message_stop ──
                    if isinstance(openai_chunk, dict) and openai_chunk.get("error") and not openai_chunk.get("choices"):
                        _err = openai_chunk.get("error")
                        _msg = _err.get("message", "") if isinstance(_err, dict) else str(_err)
                        err_data = json.dumps({
                            "type": "error",
                            "error": {"type": "api_error", "message": _msg[:500]},
                        }, ensure_ascii=False)
                        yield f"event: error\ndata: {err_data}\n\n".encode("utf-8")
                        return
                    for ev in await openai_stream_to_anthropic_events(openai_chunk, state):
                        yield format_anthropic_sse(ev)
        except Exception as e:
            # 流中出错：发出 error 事件
            err_data = json.dumps({
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": str(e)[:500],
                }
            }, ensure_ascii=False)
            yield f"event: error\ndata: {err_data}\n\n".encode("utf-8")

    # 注：不要再 del openai_response —— anthropic_stream 闭包引用它读取 body_iterator，
    # del 会让流式迭代抛 NameError（引用释放交给响应结束后 GC 即可）

    return StreamingResponse(
        anthropic_stream(),
        media_type="text/event-stream",
        headers={
            "x-msg-id": msg_id,
        }
    )


def _response_error(error: dict, status_code: int) -> JSONResponse:
    """构造 Anthropic 错误响应"""
    body = openai_error_to_anthropic(error, status_code)
    return JSONResponse(content=body, status_code=status_code)
