"""A1: Gemini generateContent ↔ OpenAI chat 双向转换。

支持范围（v1）：
- contents[].parts: text / inlineData(图片→OpenAI vision data URL) /
  functionCall / functionResponse
- systemInstruction、generationConfig（temperature/topP/maxOutputTokens/
  stopSequences/responseMimeType/thinkingConfig.thinkingBudget→effort）
- tools(functionDeclarations) 与 toolConfig(AUTO/ANY/NONE)
- 流式：消费 OpenAI SSE chunk 流 → Gemini SSE JSON 行
"""
import json
from typing import Any, Dict, List, Optional, Tuple

_FINISH_MAP = {"stop": "STOP", "length": "MAX_TOKENS", "tool_calls": "STOP",
               "content_filter": "SAFETY"}


def _parts_to_content(parts: List[dict]) -> Any:
    """parts → OpenAI content（纯文本合并为 str，含图片/混合则用数组）。"""
    texts = [p["text"] for p in parts if "text" in p]
    images = [{"type": "image_url", "image_url": {"url":
                f"data:{p['inlineData'].get('mimeType', 'image/png')};base64,{p['inlineData'].get('data', '')}"}}
              for p in parts if "inlineData" in p]
    if images:
        out = []
        if texts:
            out.append({"type": "text", "text": "\n".join(texts)})
        out.extend(images)
        return out
    return "\n".join(texts)


def gemini_to_chat(model_name: str, body: dict) -> dict:
    """Gemini 请求体 → ChatCompletionRequest 构造参数。"""
    messages: List[dict] = []
    sys_inst = body.get("systemInstruction") or body.get("system_instruction")
    if sys_inst:
        parts = sys_inst.get("parts") or []
        sys_text = "\n".join(p.get("text", "") for p in parts if "text" in p)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
    for content in body.get("contents") or []:
        role = "assistant" if (content.get("role") or "user") == "model" else "user"
        parts = content.get("parts") or []
        text_parts, tool_calls, tool_responses = [], [], []
        for p in parts:
            if "text" in p:
                text_parts.append(p)
            elif "inlineData" in p or "inline_data" in p:
                text_parts.append({"inlineData": p.get("inlineData") or p.get("inline_data")})
            elif "functionCall" in p or "function_call" in p:
                fc = p.get("functionCall") or p.get("function_call")
                tool_calls.append({
                    "id": f"call_{fc.get('name', 'fn')}",
                    "type": "function",
                    "function": {"name": fc.get("name", ""),
                                 "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False)},
                })
            elif "functionResponse" in p or "function_response" in p:
                fr = p.get("functionResponse") or p.get("function_response")
                tool_responses.append(fr)
        if tool_calls:
            messages.append({"role": "assistant",
                             "content": _parts_to_content(text_parts) if text_parts else None,
                             "tool_calls": tool_calls})
            continue
        if tool_responses:
            for fr in tool_responses:
                messages.append({"role": "tool",
                                 "tool_call_id": f"call_{fr.get('name', 'fn')}",
                                 "content": json.dumps(fr.get("response") or {}, ensure_ascii=False)})
            continue
        messages.append({"role": role, "content": _parts_to_content(text_parts)})

    gen = body.get("generationConfig") or body.get("generation_config") or {}
    kwargs: dict = {
        "model": model_name,
        "messages": messages,
        "stream": False,
    }
    if gen.get("temperature") is not None:
        kwargs["temperature"] = gen["temperature"]
    if gen.get("topP") is not None or gen.get("top_p") is not None:
        kwargs["top_p"] = gen.get("topP", gen.get("top_p"))
    if gen.get("maxOutputTokens") is not None or gen.get("max_output_tokens") is not None:
        kwargs["max_tokens"] = gen.get("maxOutputTokens", gen.get("max_output_tokens"))
    if gen.get("stopSequences"):
        kwargs["stop"] = gen["stopSequences"]
    mime = gen.get("responseMimeType") or gen.get("response_mime_type")
    if mime == "application/json":
        kwargs["extra"] = {"response_format": {"type": "json_object"}}
    # thinkingBudget → effort 档位（与 effort 后缀语义对齐）
    budget = (gen.get("thinkingConfig") or gen.get("thinking_config") or {}).get("thinkingBudget")
    if budget is not None:
        try:
            b = int(budget)
            kwargs["reasoning_effort"] = "none" if b <= 0 else (
                "low" if b <= 2048 else "medium" if b <= 8192 else "high")
        except (TypeError, ValueError):
            pass
    tools = body.get("tools") or []
    decls = []
    for t in tools:
        decls.extend(t.get("functionDeclarations") or t.get("function_declarations") or [])
    if decls:
        kwargs["tools"] = [{"type": "function", "function": d} for d in decls]
    tool_cfg = (body.get("toolConfig") or body.get("tool_config") or {}).get(
        "functionCallingConfig") or (body.get("tool_config") or {}).get("function_calling_config")
    mode = (tool_cfg or {}).get("mode", "AUTO")
    kwargs["tool_choice"] = {"AUTO": "auto", "ANY": "required", "NONE": "none"}.get(mode, "auto")
    if kwargs.get("tool_choice") == "auto" and not decls:
        kwargs.pop("tool_choice", None)
    return kwargs


def chat_json_to_gemini(payload: dict, model_version: str = "") -> dict:
    """OpenAI 非流式响应 JSON → Gemini generateContent 响应。"""
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    parts: List[dict] = []
    if msg.get("reasoning_content"):
        parts.append({"text": msg["reasoning_content"], "thought": True})
    if msg.get("content"):
        parts.append({"text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
    usage = payload.get("usage") or {}
    out = {
        "candidates": [{
            "content": {"parts": parts, "role": "model"},
            "finishReason": _FINISH_MAP.get(choice.get("finish_reason") or "stop", "STOP"),
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens",
                                         (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)),
        },
    }
    if model_version:
        out["modelVersion"] = model_version
    return out


def chunk_to_gemini(chunk: dict) -> Optional[dict]:
    """OpenAI 流式 chunk → Gemini 流式 chunk（无内容返回 None）。"""
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    parts: List[dict] = []
    if delta.get("reasoning_content"):
        parts.append({"text": delta["reasoning_content"], "thought": True})
    if delta.get("content"):
        parts.append({"text": delta["content"]})
    out: dict = {"candidates": [{"content": {"parts": parts, "role": "model"}, "index": 0}]}
    finish = choice.get("finish_reason")
    if finish:
        out["candidates"][0]["finishReason"] = _FINISH_MAP.get(finish, "STOP")
    usage = chunk.get("usage")
    if usage:
        out["usageMetadata"] = {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        }
    return out if parts or finish or usage else None


def gemini_error(status: int, message: str) -> dict:
    reason = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 404: "NOT_FOUND",
              429: "RESOURCE_EXHAUSTED", 502: "UPSTREAM_ERROR", 503: "UNAVAILABLE"}.get(
        status, "INTERNAL")
    return {"error": {"code": status, "message": message, "status": reason}}
