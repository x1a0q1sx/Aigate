"""A1: Gemini 原生协议入口（/v1beta/*）。

覆盖 Gemini CLI、LobeChat、Cherry Studio(Gemini 模式) 等原生客户端：
- GET  /v1beta/models                          模型清单（含 combo/别名可标记）
- POST /v1beta/models/{name}:generateContent   非流式
- POST /v1beta/models/{name}:streamGenerateContent?alt=sse  流式（SSE）
- POST /v1beta/models/{name}:countTokens       估算 token

实现方式：转换成内部 ChatCompletionRequest 后调用 v1 的实现体
（复用鉴权/别名/思考强度后缀/combo/auto 全部路由能力），再把
OpenAI 响应（或 SSE 流）转回 Gemini 格式。模型名支持 provider/model、
别名、-high 等思考强度后缀。
鉴权：x-goog-api-key / Bearer / x-api-key / ?key= 均可。
"""
import json
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.v1_router import (
    _chat_completions_impl,
    verify_aigate_api_key,
)
from server.core.gemini_converter import (
    chat_json_to_gemini,
    chunk_to_gemini,
    gemini_error,
    gemini_to_chat,
)
from server.schemas.chat import ChatCompletionRequest
from server.db import AsyncSessionLocal

router = APIRouter()

_ACTIONS = ("generateContent", "streamGenerateContent", "countTokens")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


def _split_action(model_name: str) -> tuple:
    base, _, action = model_name.rpartition(":")
    if not base or action not in _ACTIONS:
        return None, None
    return base, action


def _gemini_sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


async def _openai_sse_to_gemini_stream(body_iterator: AsyncIterator) -> AsyncIterator[bytes]:
    """消费 v1 的 OpenAI SSE 字节流 → Gemini SSE 流。"""
    final_usage = None
    finish = None
    async for item in body_iterator:
        if isinstance(item, bytes):
            text = item.decode("utf-8", errors="replace")
        elif isinstance(item, str):
            text = item
        else:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue  # keepalive 注释
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = {"candidates": [{"content": {"parts": [], "role": "model"},
                                        "index": 0}]}
                if finish:
                    done["candidates"][0]["finishReason"] = finish
                if final_usage:
                    done["usageMetadata"] = final_usage
                yield _gemini_sse(done)
                yield b"data: [DONE]\n\n"
                return
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            g = chunk_to_gemini(chunk)
            if not g:
                continue
            c = (g["candidates"] or [{}])[0]
            if c.get("finishReason"):
                finish = c["finishReason"]
            if g.get("usageMetadata"):
                final_usage = g["usageMetadata"]
            # 有实质内容才发；finish/usage 攒到最后
            if c.get("content", {}).get("parts"):
                yield _gemini_sse(g)


@router.get("/v1beta/models")
async def gemini_list_models(db: AsyncSession = Depends(get_db)):
    """Gemini 格式模型清单（复用 /v1/models 的数据面）"""
    from server.api.v1_router import list_models
    openai_list = await list_models(db=db, include_effort=False)
    models = []
    for m in openai_list["data"]:
        models.append({
            "name": f"models/{m['id']}",
            "displayName": m["id"],
            "supportedGenerationMethods": ["generateContent", "countTokens"],
        })
    return {"models": models}


@router.post("/v1beta/models/{model_name:path}")
async def gemini_generate(
    model_name: str,
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """generateContent / streamGenerateContent / countTokens 统一入口"""
    base, action = _split_action(model_name)
    if not base:
        return JSONResponse(status_code=400, content=gemini_error(
            400, f"模型名需以 :generateContent / :streamGenerateContent / :countTokens 结尾: '{model_name}'"))
    try:
        await verify_aigate_api_key(raw_request)
    except Exception as e:
        status = getattr(e, "status_code", 401)
        return JSONResponse(status_code=status, content=gemini_error(status, str(getattr(e, "detail", e))[:200]))
    try:
        body = await raw_request.json()
    except Exception:
        return JSONResponse(status_code=400, content=gemini_error(400, "invalid_json"))

    kwargs = gemini_to_chat(base, body)
    try:
        chat_request = ChatCompletionRequest(**kwargs)
    except Exception as e:
        return JSONResponse(status_code=400, content=gemini_error(400, f"请求转换失败: {str(e)[:200]}"))

    if action == "countTokens":
        from server.core.context_guard import estimate_request_tokens
        return {"totalTokens": estimate_request_tokens(chat_request)}

    try:
        resp = await _chat_completions_impl(chat_request, raw_request, db)
    except Exception as e:
        status = getattr(e, "status_code", 502)
        return JSONResponse(status_code=status if isinstance(status, int) else 502,
                            content=gemini_error(status if isinstance(status, int) else 502,
                                                 str(getattr(e, "detail", e))[:300]))

    if action == "generateContent":
        if not isinstance(resp, JSONResponse):
            return JSONResponse(status_code=502,
                                content=gemini_error(502, "流式上游不支持该入口，请改用 streamGenerateContent"))
        try:
            payload = json.loads(resp.body)
        except Exception:
            return JSONResponse(status_code=502, content=gemini_error(502, "上游响应解析失败"))
        if resp.status_code != 200:
            msg = str((payload.get("error") or {}).get("message") or payload)[:300]
            return JSONResponse(status_code=resp.status_code if resp.status_code >= 400 else 502,
                                content=gemini_error(resp.status_code if resp.status_code >= 400 else 502, msg))
        return JSONResponse(content=chat_json_to_gemini(payload, model_version=str(payload.get("model") or base)))

    # streamGenerateContent
    if not isinstance(resp, StreamingResponse):
        try:
            payload = json.loads(resp.body)
        except Exception:
            return JSONResponse(status_code=502, content=gemini_error(502, "上游响应解析失败"))
        # 非流式上游 → 单块返回
        g = chat_json_to_gemini(payload, model_version=str(payload.get("model") or base))
        if raw_request.query_params.get("alt") == "sse":
            return StreamingResponse((_gemini_sse(g), b"data: [DONE]\n\n"),
                                     media_type="text/event-stream")
        return JSONResponse(content=g)
    alt = raw_request.query_params.get("alt")
    if alt != "sse":
        # 未指定 alt=sse：服务端收集完整流后返回 JSON 数组（Google 原生另一种形态）
        chunks = []
        async for piece in _openai_sse_to_gemini_stream(resp.body_iterator):
            try:
                text = piece.decode("utf-8")
            except Exception:
                continue
            for line in text.splitlines():
                if line.startswith("data:") and "[DONE]" not in line:
                    try:
                        chunks.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        pass
        return JSONResponse(content=chunks)
    return StreamingResponse(
        _openai_sse_to_gemini_stream(resp.body_iterator),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
