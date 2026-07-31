"""
Codex / OpenAI Responses API adapter.

This adapter keeps AIGate's public Chat Completions surface unchanged while
calling upstreams that expect the Codex-style Responses wire format.
"""
import hashlib
import json
import time
import uuid
import logging
from typing import AsyncGenerator, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

from .base_adapter import BaseAdapter, HealthResult, ModelInfo
from server.schemas.chat import ChatCompletionRequest


def _proxy_kwargs() -> dict:
    """从代理池取 httpx 代理参数；代理池关闭时返回空 dict（即直连）"""
    from server.core.proxy_pool import get_proxy_pool
    return get_proxy_pool().proxied_kwargs()


CODEX_DEFAULT_INSTRUCTIONS = (
    "You are Codex, based on GPT-5. You are running as a coding agent in the "
    "Codex CLI on a user's computer."
)

CODEX_BUILTIN_MODELS = [
    "gpt-5.5",
    "gpt-5.5-review",
    "gpt-5.4",
    "gpt-5.4-review",
    "gpt-5.4-mini",
    "gpt-5.4-mini-review",
    "gpt-5.3-codex",
    "gpt-5.3-codex-high",
    "gpt-5.3-codex-low",
    "gpt-5.3-codex-none",
    "gpt-5.3-codex-spark",
]

# 9router / Codex Responses API 要求的 14 字段白名单（其它字段一律剥掉）
RESPONSES_API_ALLOWLIST = {
    "model", "input", "instructions", "tools", "tool_choice", "stream", "store",
    "reasoning", "service_tier", "include", "prompt_cache_key", "client_metadata",
    "text",
}

# 服务端生成的 item id 前缀（store=false 时不能引用，否则 404）
import re as _re
_SERVER_ID_PATTERN = _re.compile(r"^(rs|fc|resp|msg)_")

# Codex / OpenAI Responses 不支持的请求字段（直接删）
_RESPONSES_DENYLIST = {
    "temperature", "top_p", "frequency_penalty", "presence_penalty",
    "logprobs", "top_logprobs", "n", "seed", "max_tokens",
    "max_completion_tokens", "max_output_tokens", "user",
    "prompt_cache_retention", "metadata", "stream_options",
    "safety_identifier", "previous_response_id",
}

# 模型名后缀 -> reasoning effort
_EFFORT_LEVELS = ["none", "low", "medium", "high", "xhigh"]


def _convert_system_to_developer(body):
    items = body.get("input")
    if not isinstance(items, list):
        return
    for it in items:
        if not isinstance(it, dict) or isinstance(it, list):
            continue
        if it.get("role") == "system" and (not it.get("type") or it.get("type") == "message"):
            it["role"] = "developer"


def _strip_stored_item_references(body):
    items = body.get("input")
    if not isinstance(items, list):
        return
    body["input"] = [
        it for it in items
        if not (isinstance(it, str) and _SERVER_ID_PATTERN.match(it))
        and not (isinstance(it, dict) and it.get("type") == "item_reference")
    ]
    for it in body["input"]:
        if isinstance(it, dict):
            _id = it.get("id")
            if isinstance(_id, str) and _SERVER_ID_PATTERN.match(_id):
                it.pop("id", None)


def _apply_request_quirks(body, request):
    model = body.get("model") or (request.model if request else "")
    model_effort = None
    for level in _EFFORT_LEVELS:
        suffix = f"-{level}"
        if model.endswith(suffix):
            model_effort = level
            body["model"] = model[: -len(suffix)]
            break

    reasoning = body.get("reasoning")
    existing_effort = None
    if isinstance(reasoning, dict):
        existing_effort = reasoning.get("effort")
    explicit = body.get("reasoning_effort")
    # ???????? reasoning_effort > ???? > ?? reasoning.effort > ?? low
    if isinstance(explicit, str) and explicit in _EFFORT_LEVELS:
        effort = explicit
    elif model_effort:
        effort = model_effort
    elif existing_effort:
        effort = existing_effort
    else:
        effort = "low"

    if effort == "none":
        body.pop("reasoning", None)
    else:
        body["reasoning"] = {"effort": effort, "summary": "auto"}
        body["include"] = ["reasoning.encrypted_content"]

    body.pop("reasoning_effort", None)

    for k in _RESPONSES_DENYLIST:
        body.pop(k, None)

    for k in list(body.keys()):
        if k not in RESPONSES_API_ALLOWLIST:
            body.pop(k, None)


def normalize_responses_body(body, request=None):
    if not isinstance(body, dict):
        return body
    _convert_system_to_developer(body)
    _strip_stored_item_references(body)
    _apply_request_quirks(body, request)
    return body



class CodexResponsesAdapter(BaseAdapter):
    """Adapter for Codex-like providers that require OpenAI Responses format."""

    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self.last_proxy_url = None

    def _proxy(self) -> dict:
        """取代理参数并记下本次线请求实际使用的代理 URL（写入 ContextVar，供日志落库）。"""
        pk = _proxy_kwargs()
        url = pk.get("proxy")
        self.last_proxy_url = url
        from server.core.proxy_pool import CURRENT_PROXY_URL
        CURRENT_PROXY_URL.set(url)
        return pk

    def _build_url(self, base_url: str, compact: bool = False) -> str:
        base = (base_url or "").rstrip("/")
        if not base:
            raise ValueError("base_url is required")
        if base.endswith("/responses") or base.endswith("/responses/compact"):
            url = base
        elif base.endswith("/chat/completions"):
            url = base[: -len("/chat/completions")] + "/responses"
        elif base.endswith("/v1"):
            url = f"{base}/responses"
        else:
            url = f"{base}/v1/responses"
        if compact and not url.endswith("/compact"):
            url = f"{url}/compact"
        return url

    def _build_models_url(self, base_url: str) -> Optional[str]:
        base = (base_url or "").rstrip("/")
        if not base or "backend-api/codex" in base:
            return None
        if base.endswith("/responses"):
            base = base[: -len("/responses")]
        elif base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}/models"

    def _session_id(self, request: ChatCompletionRequest) -> str:
        raw = json.dumps(request.model_dump(exclude_none=True), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"aigate_{digest}"

    def _get_headers(
        self,
        api_key: str,
        request: ChatCompletionRequest,
        extra_headers: dict = None,
    ) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "codex_cli_rs/0.136.0",
            "originator": "codex_cli_rs",
            "session_id": self._session_id(request),
            # 模拟 Codex 官方客户端：部分上游（如 zzzcoding 公益站）按此头放行，
            # 缺失则返回 403 "This account only allows Codex official clients"
            "X-Codex-Client": "official",
            "OpenAI-Beta": "responses=v1",
        }
        if api_key and str(api_key).strip():
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        if not headers.get("originator"):
            headers["originator"] = "codex_cli_rs"
        if not headers.get("session_id"):
            headers["session_id"] = self._session_id(request)
        return headers

    def _content_blocks(self, role: str, content) -> list:
        text_type = "output_text" if role == "assistant" else "input_text"
        if content is None:
            return []
        if isinstance(content, str):
            return [{"type": text_type, "text": content}]
        if not isinstance(content, list):
            return [{"type": text_type, "text": json.dumps(content, ensure_ascii=False)}]

        blocks = []
        for item in content:
            if isinstance(item, str):
                blocks.append({"type": text_type, "text": item})
                continue
            if not isinstance(item, dict):
                blocks.append({"type": text_type, "text": json.dumps(item, ensure_ascii=False)})
                continue
            item_type = item.get("type")
            if item_type == "text":
                blocks.append({"type": text_type, "text": item.get("text", "")})
            elif item_type in ("image_url", "input_image"):
                image = item.get("image_url", "")
                if isinstance(image, dict):
                    image_url = image.get("url", "")
                    detail = image.get("detail", item.get("detail", "auto"))
                else:
                    image_url = image
                    detail = item.get("detail", "auto")
                blocks.append({"type": "input_image", "image_url": image_url, "detail": detail})
            else:
                blocks.append({"type": text_type, "text": item.get("text") or json.dumps(item, ensure_ascii=False)})
        return blocks

    def _convert_tools(self, tools) -> Optional[list]:
        """Flatten OpenAI Chat-Completions tools into Codex Responses format."""
        if not tools:
            return None
        converted = []
        passthrough_types = {"custom"}
        hosted_types = {
            "image_generation", "web_search", "web_search_preview", "file_search",
            "computer", "computer_use_preview", "code_interpreter", "mcp",
            "local_shell", "tool_search",
        }
        for tool in tools:
            if not isinstance(tool, dict) or isinstance(tool, list):
                continue
            t = tool.get("type")
            if isinstance(t, str) and t != "function":
                if t in passthrough_types:
                    converted.append(tool); continue
                if t in hosted_types:
                    converted.append(tool); continue
                if not tool.get("function") and not isinstance(tool.get("name"), str):
                    continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
            raw_name = tool.get("name") if isinstance(tool.get("name"), str) else (fn.get("name") if fn else "")
            name = (raw_name or "").strip()
            if not name:
                continue
            description = tool.get("description") if isinstance(tool.get("description"), str) else (fn.get("description") if fn else "")
            params = tool.get("parameters")
            if not (isinstance(params, dict) and not isinstance(params, list)):
                params = (fn.get("parameters") if fn else None) or {"type": "object", "properties": {}}
            item = {
                "type": "function",
                "name": name[:128],
                "parameters": params,
            }
            if description:
                item["description"] = description
            if fn and isinstance(fn.get("strict"), bool):
                item["strict"] = fn["strict"]
            converted.append(item)
        return converted or None

    def _normalize_tool_choice(self, tool_choice, tools):
        if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
            return tool_choice
        valid = {t.get("name") for t in tools if isinstance(t, dict)}
        fn = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else {}
        name = fn.get("name")
        if name and name not in valid:
            return None
        return tool_choice

    def _build_responses_payload(self, request: ChatCompletionRequest) -> dict:
        input_items = []
        instructions = []
        last_assistant_call_id = None

        for message in request.messages or []:
            role = message.role
            if role == "system":
                if isinstance(message.content, str) and message.content:
                    instructions.append(message.content)
                continue

            if role in ("user", "assistant", "developer"):
                blocks = self._content_blocks(role, message.content)
                if blocks:
                    input_items.append({"type": "message", "role": role, "content": blocks})

            if role == "assistant" and message.tool_calls:
                for call in message.tool_calls:
                    fn = call.get("function", {}) if isinstance(call, dict) else {}
                    call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")[:64]
                    last_assistant_call_id = call_id
                    input_items.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": fn.get("name") or "_unknown",
                        "arguments": fn.get("arguments") or "{}",
                    })

            if role == "tool":
                output = message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False)
                # 优先配对前一条 assistant function_call 的 call_id；缺失则合成稳定占位。
                # （OpenAI Responses API 要求 function_call_output.call_id 与对应 function_call 一致）
                call_id = str(message.tool_call_id or last_assistant_call_id or f"call_{uuid.uuid4().hex}")[:64]
                if not message.tool_call_id:
                    logger.warning(
                        "Codex adapter: tool message missing tool_call_id, paired with %s", call_id
                    )
                input_items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                })

        if not input_items:
            input_items.append({
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "..."}],
            })

        payload = {
            "model": request.model,
            "input": input_items,
            "instructions": "\n\n".join(instructions).strip() or CODEX_DEFAULT_INSTRUCTIONS,
            "stream": True,
            "store": False,
            "reasoning": {"effort": "low", "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": self._session_id(request),
        }
        # ?????????? reasoning_effort / reasoning?? normalize ????????
        if getattr(request, "reasoning_effort", None):
            payload["reasoning_effort"] = request.reasoning_effort
        if getattr(request, "reasoning", None) and isinstance(request.reasoning, dict):
            payload["reasoning"] = {**payload["reasoning"], **request.reasoning}

        tools = self._convert_tools(request.tools)
        # Codex Responses 规范化：白名单 + 后缀解析 effort + 剥多余字段
        normalize_responses_body(payload, request)

        if tools:
            payload["tools"] = tools
        if request.tool_choice is not None:
            tc = self._normalize_tool_choice(request.tool_choice, payload.get("tools") or [])
            if tc is not None:
                payload["tool_choice"] = tc
        return payload

    async def _iter_sse_events(self, response: httpx.Response) -> AsyncGenerator[Tuple[Optional[str], str], None]:
        event = None
        data_lines = []
        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")
            if not line:
                if data_lines:
                    yield event, "\n".join(data_lines)
                    event = None
                    data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield event, "\n".join(data_lines)

    def _chunks_from_response_json(self, data: dict, request: ChatCompletionRequest) -> List[dict]:
        text_parts = []
        reasoning_parts = []
        tool_calls = []
        for item in data.get("output", []) or []:
            if item.get("type") == "reasoning":
                for summary in item.get("summary", []) or []:
                    if summary.get("text"):
                        reasoning_parts.append(summary["text"])
            elif item.get("type") == "function_call":
                tool_calls.append({
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "",
                    },
                })
            for content in item.get("content", []) or []:
                if content.get("text"):
                    text_parts.append(content["text"])
        text = "".join(text_parts) or data.get("output_text") or ""
        message = {"role": "assistant", "content": text if text else None}
        if reasoning_parts:
            message["reasoning_content"] = "\n".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"
        return [{
            "id": data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(data.get("created_at") or time.time()),
            "model": request.model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": self._usage_from_responses(data.get("usage") or {}),
        }]

    def _usage_from_responses(self, usage: dict) -> dict:
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        return {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            # 透传缓存明细，供 v1_router 计费时提取缓存 token
            "input_tokens_details": usage.get("input_tokens_details") or {},
            "cache_creation_details": usage.get("cache_creation_details") or {},
        }

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
    ) -> dict:
        content_parts = []
        reasoning_parts = []
        usage = {}
        response_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        created = int(time.time())
        # 累积 tool_calls：index -> {id, type, function:{name, arguments}}
        tool_calls_acc = {}
        final_finish = "stop"

        async for chunk in self.stream_chat_completion(request, api_key, base_url, extra_headers):
            if "error" in chunk and "choices" not in chunk:
                raise RuntimeError(str(chunk["error"])[:500])
            response_id = chunk.get("id", response_id)
            created = int(chunk.get("created") or created)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices", []) or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    acc = tool_calls_acc.setdefault(idx, {
                        "id": None, "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    if tc.get("type"):
                        acc["type"] = tc["type"]
                    fn = tc.get("function") or {}
                    if fn.get("name") is not None:
                        acc["function"]["name"] += fn["name"]
                    if fn.get("arguments") is not None:
                        acc["function"]["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    final_finish = choice["finish_reason"]

        tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        message = {"role": "assistant", "content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
            # OpenAI 语义：有 tool_calls 时 content 应为 null（除非模型同时产出了文本）
            if not "".join(content_parts).strip():
                message["content"] = None
        finish = "tool_calls" if tool_calls else final_finish
        return {
            "id": response_id,
            "object": "chat.completion",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
    ) -> AsyncGenerator[dict, None]:
        url = self._build_url(base_url, compact=request.model.endswith("-openai-compact"))
        headers = self._get_headers(api_key, request, extra_headers)
        payload = self._build_responses_payload(request)
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        created = int(time.time())
        sent_role = False
        final_usage = {}
        # 工具调用累积：call_id -> {index, name, args}；标记是否出现过 function_call
        active_calls = {}
        saw_tool_call = False

        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy()) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise httpx.HTTPStatusError(
                        f"Client error '{resp.status_code} {resp.reason_phrase}' for url '{url}'\nResponse: {body[:500]}",
                        request=resp.request,
                        response=resp,
                    )

                content_type = resp.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    body = await resp.aread()
                    data = json.loads(body.decode("utf-8", errors="replace") or "{}")
                    if isinstance(data, dict) and data.get("choices"):
                        yield data
                        return
                    for item in self._chunks_from_response_json(data, request):
                        yield item
                    return

                async for event, data_text in self._iter_sse_events(resp):
                    if data_text == "[DONE]":
                        break
                    try:
                        data = json.loads(data_text)
                    except json.JSONDecodeError:
                        continue

                    event_type = event or data.get("type") or ""
                    if data.get("id") and str(data.get("id")).startswith("resp"):
                        chunk_id = data["id"].replace("resp", "chatcmpl", 1)
                    if data.get("created_at"):
                        created = int(data["created_at"])

                    if data.get("choices"):
                        yield data
                        continue

                    if event_type in ("response.failed", "error") or data.get("error"):
                        yield {"error": data.get("error") or data}
                        break

                    # ── 工具调用：Responses function_call → OpenAI tool_calls ──
                    if event_type == "response.output_item.added":
                        item = data.get("item") or {}
                        if item.get("type") == "function_call":
                            call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"
                            idx = len(active_calls)
                            active_calls[call_id] = {
                                "index": idx,
                                "name": item.get("name") or "",
                                "args": item.get("arguments") or "",
                            }
                            saw_tool_call = True
                            delta = {
                                "tool_calls": [{
                                    "index": idx,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": item.get("name") or "", "arguments": ""},
                                }]
                            }
                            if not sent_role:
                                delta["role"] = "assistant"
                                sent_role = True
                            yield {
                                "id": chunk_id, "object": "chat.completion.chunk",
                                "created": created, "model": request.model,
                                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                            }
                        continue

                    if event_type == "response.function_call_arguments.delta":
                        call_id = data.get("call_id") or data.get("item_id")
                        if call_id in active_calls:
                            frag = data.get("delta") or ""
                            active_calls[call_id]["args"] += frag
                            yield {
                                "id": chunk_id, "object": "chat.completion.chunk",
                                "created": created, "model": request.model,
                                "choices": [{"index": 0, "delta": {
                                    "tool_calls": [{
                                        "index": active_calls[call_id]["index"],
                                        "function": {"arguments": frag},
                                    }]
                                }, "finish_reason": None}],
                            }
                        continue

                    if event_type == "response.output_item.done":
                        item = data.get("item") or {}
                        if item.get("type") == "function_call":
                            call_id = item.get("call_id") or item.get("id")
                            if call_id in active_calls:
                                full = item.get("arguments") or ""
                                # 仅当流式 delta 未覆盖时补发完整参数（避免重复）
                                if active_calls[call_id]["args"] != full:
                                    active_calls[call_id]["args"] = full
                                    yield {
                                        "id": chunk_id, "object": "chat.completion.chunk",
                                        "created": created, "model": request.model,
                                        "choices": [{"index": 0, "delta": {
                                            "tool_calls": [{
                                                "index": active_calls[call_id]["index"],
                                                "function": {"arguments": full},
                                            }]
                                        }, "finish_reason": None}],
                                    }
                        continue

                    if event_type.endswith(".delta"):
                        delta_text = data.get("delta") or data.get("text") or ""
                        if not delta_text:
                            continue
                        delta = {}
                        if not sent_role:
                            delta["role"] = "assistant"
                            sent_role = True
                        if "reasoning" in event_type:
                            delta["reasoning_content"] = delta_text
                        else:
                            delta["content"] = delta_text
                        yield {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": request.model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        }
                    elif event_type in ("response.completed", "response.done"):
                        response_obj = data.get("response") if isinstance(data.get("response"), dict) else data
                        final_usage = self._usage_from_responses(response_obj.get("usage") or {})
                        # 兜底：从 output 补全流式未覆盖到的工具调用
                        for item in response_obj.get("output", []) or []:
                            if item.get("type") != "function_call":
                                continue
                            call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"
                            if call_id in active_calls:
                                continue
                            idx = len(active_calls)
                            active_calls[call_id] = {
                                "index": idx,
                                "name": item.get("name") or "",
                                "args": item.get("arguments") or "",
                            }
                            saw_tool_call = True
                            delta = {
                                "tool_calls": [{
                                    "index": idx,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": item.get("name") or "",
                                        "arguments": item.get("arguments") or "",
                                    },
                                }]
                            }
                            if not sent_role:
                                delta["role"] = "assistant"
                                sent_role = True
                            yield {
                                "id": chunk_id, "object": "chat.completion.chunk",
                                "created": created, "model": request.model,
                                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                            }

        finish = "tool_calls" if saw_tool_call else "stop"
        yield {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": final_usage,
        }

    async def list_models(
        self,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
    ) -> List[ModelInfo]:
        url = self._build_models_url(base_url)
        if not url:
            return [ModelInfo(model_id=m, display_name=m, supports_streaming=True) for m in CODEX_BUILTIN_MODELS]
        headers = {
            "Accept": "application/json",
            "User-Agent": "codex_cli_rs/0.136.0",
            "originator": "codex_cli_rs",
            # 模拟 Codex 官方客户端，部分上游按此头放行
            "X-Codex-Client": "official",
            "OpenAI-Beta": "responses=v1",
        }
        if api_key and str(api_key).strip():
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy()) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400 and "backend-api/codex" in (base_url or ""):
                return [ModelInfo(model_id=m, display_name=m, supports_streaming=True) for m in CODEX_BUILTIN_MODELS]
            resp.raise_for_status()
            data = resp.json()
        return [
            ModelInfo(model_id=item.get("id", ""), display_name=item.get("id", ""), supports_streaming=True)
            for item in data.get("data", [])
            if item.get("id")
        ]

    async def health_check(
        self,
        model: str,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
        timeout: int = 10,
    ) -> HealthResult:
        req = ChatCompletionRequest(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            stream=True,
            max_tokens=1,
        )
        start = time.time()
        try:
            adapter = CodexResponsesAdapter(timeout=timeout)
            async for chunk in adapter.stream_chat_completion(req, api_key, base_url, extra_headers):
                if "error" in chunk and "choices" not in chunk:
                    raise RuntimeError(str(chunk["error"])[:200])
                if chunk.get("choices"):
                    break
            latency_ms = (time.time() - start) * 1000
            from server.config import get_config
            threshold = get_config().health_check.healthy_latency_threshold_ms
            return HealthResult(
                status="healthy" if latency_ms < threshold else "degraded",
                latency_ms=latency_ms,
                error_message="",
            )
        except httpx.HTTPStatusError as e:
            latency_ms = (time.time() - start) * 1000
            status = "rate_limited" if e.response.status_code == 429 else "unhealthy"
            return HealthResult(status=status, latency_ms=latency_ms, error_message=f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            return HealthResult(status="unhealthy", latency_ms=latency_ms, error_message=str(e)[:200])
