"""
Anthropic Claude Messages API adapter.

Supports two auth modes:
- api_key mode (x-api-key header) — official Anthropic API
- oauth mode (Authorization: Bearer) — Claude Code Pro/Max subscription

Injects full Claude CLI fingerprint headers (anthropic-beta + X-Stainless)
so third-party Anthropic-compatible reverse proxies that gate on client
identity accept the request.

Translates OpenAI Chat Completions -> Anthropic Messages format:
- system messages separated into top-level `system` field
- tools converted to Anthropic input_schema format
- tool_calls / tool responses mapped to Anthropic content blocks
- streaming events translated back to OpenAI chat.completion.chunk
"""
import json
import time
import uuid
import platform
from typing import AsyncGenerator, List, Optional, Any

import httpx


def _proxy_kwargs() -> dict:
    """从代理池取 httpx 代理参数；代理池关闭时返回空 dict（即直连）"""
    from server.core.proxy_pool import get_proxy_pool
    return get_proxy_pool().proxied_kwargs()

from .base_adapter import BaseAdapter, ModelInfo, HealthResult
from server.schemas.chat import ChatCompletionRequest


ANTHROPIC_API_VERSION = "2023-06-01"

CLAUDE_CLI_FINGERPRINT = {
    "User-Agent": "claude-cli/2.1.92 (external, sdk-cli)",
    "X-App": "cli",
    "Anthropic-Beta": (
        "claude-code-20250219,oauth-2025-04-20,"
        "interleaved-thinking-2025-05-14,"
        "context-management-2025-06-27,"
        "prompt-caching-scope-2026-01-05,"
        "advanced-tool-use-2025-11-20,"
        "effort-2025-11-24,"
        "structured-outputs-2025-12-15,"
        "fast-mode-2026-02-01,"
        "redact-thinking-2026-02-12,"
        "token-efficient-tools-2026-03-28,"
        "context-1m-2025-08-07"
    ),
    "Anthropic-Dangerous-Direct-Browser-Access": "true",
    "X-Stainless-Helper-Method": "stream",
    "X-Stainless-Retry-Count": "0",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Lang": "js",
    "X-Stainless-Package-Version": "0.80.0",
    "X-Stainless-Timeout": "600",
}

ANTHROPIC_MINIMAL_BETA = "claude-code-20250219,interleaved-thinking-2025-05-14"


def _stainless_arch() -> str:
    a = platform.machine().lower()
    if a in ("amd64", "x86_64"):
        return "x64"
    if a in ("arm64", "aarch64"):
        return "arm64"
    if a in ("i386", "i686", "x86"):
        return "x86"
    return f"other::{a}"


def _stainless_os() -> str:
    n = platform.system().lower()
    if n == "windows":
        return "Windows"
    if n == "darwin":
        return "MacOS"
    if n == "linux":
        return "Linux"
    return n or "Other"


CLAUDE_CLI_FINGERPRINT["X-Stainless-Arch"] = _stainless_arch()
CLAUDE_CLI_FINGERPRINT["X-Stainless-Os"] = _stainless_os()
CLAUDE_CLI_FINGERPRINT["X-Stainless-Runtime-Version"] = "v24.14.0"
CLAUDE_BUILTIN_MODELS = [
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-opus-4-7", "Claude Opus 4.7"),
    ("claude-opus-4-6", "Claude Opus 4.6"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-opus-4-5-20251101", "Claude 4.5 Opus"),
    ("claude-sonnet-4-5-20250929", "Claude 4.5 Sonnet"),
    ("claude-haiku-4-5-20251001", "Claude 4.5 Haiku"),
    ("claude-sonnet-4-20250514", "Claude Sonnet 4"),
    ("claude-opus-4-20250514", "Claude Opus 4"),
    ("claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet"),
    ("claude-3-opus-20240229", "Claude 3 Opus"),
]


class AnthropicAdapter(BaseAdapter):
    """Anthropic Messages API adapter (api_key or oauth)."""

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

    def _strip_prefix(self, model: str, provider_name: str = "") -> str:
        """去掉 `provider/model` 前缀，只保留 Claude 模型名。"""
        m = (model or "").strip()
        if "/" in m:
            m = m.rsplit("/", 1)[-1]
        return m

    def _build_url(self, base_url: str) -> str:
        base = (base_url or "").rstrip("/")
        if not base:
            base = "https://api.anthropic.com"
        if base.endswith("/v1/messages") or base.endswith("/messages"):
            return f"{base}?beta=true"
        if base.endswith("/v1"):
            return f"{base}/messages?beta=true"
        return f"{base}/v1/messages?beta=true"

    def _get_headers(self, api_key: str, extra_headers: dict = None, oauth: bool = False, full_fingerprint: bool = True) -> dict:
        is_third_party = False  # 由 caller 通过 extra_headers 间接判断
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream,application/json",
            "anthropic-version": ANTHROPIC_API_VERSION,
        }
        if full_fingerprint:
            headers.update(CLAUDE_CLI_FINGERPRINT)
        else:
            headers["Anthropic-Beta"] = ANTHROPIC_MINIMAL_BETA
            headers["User-Agent"] = "anthropic-python/0.40.0"

        if oauth:
            if api_key and str(api_key).strip():
                headers["Authorization"] = f"Bearer {api_key}"
        else:
            if api_key and str(api_key).strip():
                headers["x-api-key"] = api_key
        if extra_headers:
            headers.update(extra_headers)
            # 第三方反代常见的适配：用户显式带了 base_url 又带了 x-api-key，补 Bearer
            if not oauth and api_key and str(api_key).strip() and not headers.get("Authorization"):
                # 仅当 base_url 看起来不是官方 anthropic 时，补 Bearer（由 caller 注入 extra_headers["__baseUrl"]）
                base = extra_headers.get("__baseUrl") or ""
                if base and "api.anthropic.com" not in base:
                    headers["Authorization"] = f"Bearer {api_key}"
        headers.pop("__baseUrl", None)
        headers.pop("__oauth", None)
        return headers

    def _content_blocks(self, content: Any) -> list:
        """OpenAI content (str | list) -> Anthropic content blocks。"""
        if content is None:
            return []
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return [{"type": "text", "text": json.dumps(content, ensure_ascii=False)}]
        blocks = []
        for item in content:
            if isinstance(item, str):
                blocks.append({"type": "text", "text": item})
                continue
            if not isinstance(item, dict):
                blocks.append({"type": "text", "text": json.dumps(item, ensure_ascii=False)})
                continue
            t = item.get("type")
            if t == "text":
                blocks.append({"type": "text", "text": item.get("text", "")})
            elif t in ("image_url", "input_image", "image"):
                img = item.get("image_url") or item.get("image") or {}
                url = img.get("url") if isinstance(img, dict) else img
                if isinstance(url, str) and url.startswith("data:"):
                    header, _, data = url.partition(",")
                    media = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
                elif isinstance(url, str):
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            else:
                blocks.append({"type": "text", "text": item.get("text") or json.dumps(item, ensure_ascii=False)})
        return blocks

    def _convert_tools(self, tools: Optional[list]) -> Optional[list]:
        if not tools:
            return None
        out = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = (fn.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "name": name,
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        return out or None

    def _convert_tool_choice(self, tc: Any) -> Any:
        if tc is None:
            return None
        if isinstance(tc, str) and tc == "auto":
            return {"type": "auto"}
        if isinstance(tc, str) and tc == "none":
            return {"type": "none"}
        if isinstance(tc, str) and tc == "required":
            return {"type": "any"}
        if isinstance(tc, dict) and tc.get("type") == "function":
            fn = tc.get("function") or {}
            return {"type": "tool", "name": fn.get("name")}
        return {"type": "auto"}
    def _build_payload(self, request: ChatCompletionRequest, provider_name: str = "", stream: bool = False) -> dict:
        messages = []
        system_parts = []
        for m in request.messages or []:
            role = m.role
            content = m.content
            if role == "system":
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                if text and text.strip():
                    system_parts.append({"type": "text", "text": text})
                continue
            if role == "tool":
                tc_msg = {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": str(m.tool_call_id or f"call_{uuid.uuid4().hex}")[:64],
                        "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                    }],
                }
                messages.append(tc_msg)
                continue
            role_claude = "assistant" if role == "assistant" else "user"
            blocks = self._content_blocks(content)
            if role == "assistant" and m.tool_calls:
                for call in m.tool_calls:
                    fn = call.get("function", {}) if isinstance(call, dict) else {}
                    blocks.append({
                        "type": "tool_use",
                        "id": str(call.get("id") or f"call_{uuid.uuid4().hex}")[:64],
                        "name": fn.get("name") or "_unknown",
                        "input": self._safe_json(fn.get("arguments") or "{}"),
                    })
            if blocks:
                messages.append({"role": role_claude, "content": blocks})

        if not messages:
            messages.append({"role": "user", "content": [{"type": "text", "text": "..."}]})

        payload = {
            "model": self._strip_prefix(request.model, provider_name),
            "messages": messages,
            "max_tokens": int(getattr(request, "max_tokens", 0) or 1024),
            "stream": stream,
        }
        if system_parts:
            payload["system"] = system_parts
        tools = self._convert_tools(getattr(request, "tools", None))
        if tools:
            payload["tools"] = tools
        tc = self._convert_tool_choice(getattr(request, "tool_choice", None))
        if tc is not None:
            payload["tool_choice"] = tc
        if getattr(request, "temperature", None) is not None:
            payload["temperature"] = request.temperature
        if getattr(request, "top_p", None) is not None:
            payload["top_p"] = request.top_p
        thinking_spec = getattr(request, "reasoning", None)
        if isinstance(thinking_spec, dict) and thinking_spec.get("type"):
            payload["thinking"] = thinking_spec
        return payload

    def _safe_json(self, s: Any) -> dict:
        if isinstance(s, dict):
            return s
        if isinstance(s, str):
            try:
                return json.loads(s)
            except Exception:
                return {}
        return {}

    def _result_from_messages(self, url: str, resp_json: dict, request: ChatCompletionRequest, created: int) -> dict:
        content_text = []
        content_blocks = []
        for i, b in enumerate(resp_json.get("content", []) or []):
            if b.get("type") == "text":
                content_text.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                content_blocks.append({
                    "id": b.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                    },
                })
        message = {"role": "assistant", "content": "".join(content_text)}
        if content_blocks:
            message["tool_calls"] = content_blocks
        stop_reason = resp_json.get("stop_reason")
        finish = "tool_calls" if stop_reason == "tool_use" else "stop"
        usage = resp_json.get("usage") or {}
        return {
            "id": resp_json.get("id") or f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            },
        }
    async def chat_completion(self, request: ChatCompletionRequest, api_key: str, base_url: str, extra_headers: dict = None) -> dict:
        url = self._build_url(base_url)
        oauth = bool(extra_headers and extra_headers.get("__oauth"))
        eh = {k: v for k, v in (extra_headers or {}).items() if k != "__oauth"}
        eh["__baseUrl"] = base_url or ""
        headers = self._get_headers(api_key, eh, oauth=oauth, full_fingerprint=True)
        payload = self._build_payload(request, "", stream=False)
        created = int(time.time())
        async with httpx.AsyncClient(timeout=self.timeout * 5, **self._proxy()) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}: {resp.text[:500]}",
                    request=resp.request, response=resp,
                )
            return self._result_from_messages(url, resp.json(), request, created)

    async def stream_chat_completion(self, request: ChatCompletionRequest, api_key: str, base_url: str, extra_headers: dict = None) -> AsyncGenerator[dict, None]:
        url = self._build_url(base_url)
        oauth = bool(extra_headers and extra_headers.get("__oauth"))
        eh = {k: v for k, v in (extra_headers or {}).items() if k != "__oauth"}
        eh["__baseUrl"] = base_url or ""
        headers = self._get_headers(api_key, eh, oauth=oauth, full_fingerprint=True)
        payload = self._build_payload(request, "", stream=True)
        created = int(time.time())
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        model_name = request.model
        # tool_use 累积
        tool_buf = {}
        text_done_marker = False
        _produced = False  # 整个流是否产出过任何有效事件（内容/思考/工具/usage）
        async with httpx.AsyncClient(timeout=(5, self.timeout * 5), **self._proxy()) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {body.decode('utf-8', errors='replace')[:500]}",
                        request=resp.request, response=resp,
                    )
                # 上游若返回非 SSE 实体（典型：代理出口被 WAF/验证码拦截返回的 HTML 页），
                # 直接判错，避免网关把空响应记成"成功"导致客户端反复重试 / 死循环。
                _ct = (resp.headers.get("content-type") or "").lower()
                if "text/event-stream" not in _ct and "application/json" not in _ct:
                    body = await resp.aread()
                    raise httpx.HTTPStatusError(
                        f"upstream returned non-SSE response (content-type={_ct or 'unknown'}): "
                        f"{body.decode('utf-8', errors='replace')[:300]}",
                        request=resp.request, response=resp,
                    )
                current_block_idx = -1
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].lstrip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    evt = data.get("type")
                    if evt == "message_start":
                        msg = data.get("message") or {}
                        for c in (msg.get("content") or []):
                            idx = c.get("index", 0)
                            current_block_idx = idx
                        continue
                    if evt == "content_block_start":
                        c = data.get("content_block") or {}
                        if c.get("type") == "tool_use":
                            # 部分 Anthropic 兼容上游（如 agentrouter）会把工具调用的
                            # 完整 input 直接放在 content_block_start 的 input 字段里，
                            # 而不是靠后续的 input_json_delta 分片下发。这里要把它种进去，
                            # 否则参数会丢失（落库成 {}，客户端也拿到空参数）。
                            # 仅当 input 为非空字典时才种入；空 dict/缺失保持 ""，
                            # 以兼容真 Anthropic 用 input_json_delta 逐片追加的机制。
                            _seed = c.get("input")
                            if isinstance(_seed, dict):
                                _seed = json.dumps(_seed, ensure_ascii=False) if len(_seed) > 0 else ""
                            elif _seed is None:
                                _seed = ""
                            else:
                                _seed = str(_seed)
                            tool_buf[c.get("index", 0)] = {
                                "id": c.get("id", ""),
                                "name": c.get("name", ""),
                                "input": _seed,
                            }
                        continue
                    if evt == "content_block_delta":
                        delta = data.get("delta") or {}
                        dt = delta.get("type")
                        cb_idx = data.get("index", current_block_idx)
                        if dt == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                _produced = True
                                yield {
                                    "id": chunk_id, "object": "chat.completion.chunk",
                                    "created": created, "model": model_name,
                                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                                }
                        elif dt == "input_json_delta":
                            if cb_idx in tool_buf:
                                tool_buf[cb_idx]["input"] += delta.get("partial_json", "")
                        elif dt == "thinking_delta":
                            t = delta.get("thinking", "")
                            if t:
                                _produced = True
                                yield {
                                    "id": chunk_id, "object": "chat.completion.chunk",
                                    "created": created, "model": model_name,
                                    "choices": [{"index": 0, "delta": {"reasoning_content": t}, "finish_reason": None}],
                                }
                        continue
                    if evt == "content_block_stop":
                        continue
                    if evt == "message_delta":
                        d = data.get("delta") or {}
                        sr = d.get("stop_reason")
                        if sr:
                            tool_calls = []
                            if tool_buf:
                                tool_calls = [{
                                    "id": v["id"], "type": "function", "index": i,
                                    "function": {"name": v["name"], "arguments": v["input"] or "{}"},
                                } for i, v in sorted(tool_buf.items())]
                            finish = "tool_calls" if (sr == "tool_use" and tool_calls) else "stop"
                            chunk = {
                                "id": chunk_id, "object": "chat.completion.chunk",
                                "created": created, "model": model_name,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                            }
                            if tool_calls:
                                chunk["choices"][0]["delta"]["tool_calls"] = tool_calls
                            _produced = True
                            yield chunk
                        usage = (data.get("usage") or {})
                        if usage:
                            _produced = True
                            yield {
                                "id": chunk_id, "object": "chat.completion.chunk",
                                "created": created, "model": model_name,
                                "choices": [],
                                "usage": {
                                    "prompt_tokens": int(usage.get("input_tokens") or 0),
                                    "completion_tokens": int(usage.get("output_tokens") or 0),
                                    "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
                                    "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                                    "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                                },
                            }
                        continue
                    if evt == "message_stop":
                        continue
                    if evt == "error":
                        err = data.get("error") or {}
                        yield {"error": err.get("message") or "upstream error"}
                        break
        if _produced:
            yield {
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        else:
            # 整个流没有任何有效事件（内容/思考/工具/usage），视为上游空/失败响应
            # （典型：代理出口被 WAF/验证码拦截）。判为错误，让网关记为 error，
            # 避免客户端把"空成功"当成完成而无限重试。
            yield {"error": "upstream returned empty stream (no SSE events; possible proxy/WAF block)"}
    def _builtin_models(self) -> List[ModelInfo]:
        return [
            ModelInfo(model_id=mid, display_name=name, input_price=15.0 if "opus" in mid else 3.0,
                      output_price=75.0 if "opus" in mid else 15.0, is_free=False, supports_streaming=True)
            for mid, name in CLAUDE_BUILTIN_MODELS
        ]

    async def list_models(self, api_key: str, base_url: str, extra_headers: dict = None) -> List[ModelInfo]:
        """真实请求上游 /v1/models（带 Claude CLI 指纹头，第三方中转如 agentrouter 会校验客户端指纹）。
        失败/空结果时回退内置 Claude 列表，保证官方 API 或不支持 /v1/models 的站仍可用。"""
        base = (base_url or "").rstrip("/")
        if not base:
            return self._builtin_models()
        # 归一化出站点 origin：剥掉 /v1/messages、/messages、/v1 尾巴
        for suffix in ("/v1/messages", "/messages", "/v1"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        oauth = bool(extra_headers and extra_headers.get("__oauth"))
        eh = {k: v for k, v in (extra_headers or {}).items() if k != "__oauth"}
        eh["__baseUrl"] = base_url or ""
        headers = self._get_headers(api_key, eh, oauth=oauth, full_fingerprint=True)
        headers.pop("Content-Type", None)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, **self._proxy()) as client:
                resp = await client.get(f"{base}/v1/models", headers=headers)
                if resp.status_code != 200:
                    print(f"[anthropic] list_models {base} -> HTTP {resp.status_code}，回退内置列表")
                    return self._builtin_models()
                data = resp.json()
                items = data.get("data", data if isinstance(data, list) else [])
                models = []
                for it in items:
                    mid = (it.get("id") or "").strip() if isinstance(it, dict) else ""
                    if not mid:
                        continue
                    models.append(ModelInfo(
                        model_id=mid,
                        display_name=(it.get("display_name") or mid) if isinstance(it, dict) else mid,
                        input_price=15.0 if "opus" in mid else 3.0,
                        output_price=75.0 if "opus" in mid else 15.0,
                        is_free=False,
                        supports_streaming=True,
                    ))
                if not models:
                    return self._builtin_models()
                return models
        except Exception as exc:
            print(f"[anthropic] list_models {base} 失败：{type(exc).__name__}: {exc}，回退内置列表")
            return self._builtin_models()

    async def health_check(self, model: str, api_key: str, base_url: str, extra_headers: dict = None, timeout: int = 10) -> HealthResult:
        url = self._build_url(base_url)
        oauth = bool(extra_headers and extra_headers.get("__oauth"))
        eh = {k: v for k, v in (extra_headers or {}).items() if k != "__oauth"}
        eh["__baseUrl"] = base_url or ""
        headers = self._get_headers(api_key, eh, oauth=oauth, full_fingerprint=True)
        payload = {
            "model": self._strip_prefix(model, ""),
            "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
            "max_tokens": 1,
            "stream": False,
        }
        start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=timeout * 5, **self._proxy()) as client:
                resp = await client.post(url, headers=headers, json=payload)
                latency_ms = (time.time() - start_time) * 1000
                if resp.status_code == 429:
                    return HealthResult(status="rate_limited", latency_ms=latency_ms, error_message="Rate limit exceeded")
                if resp.status_code >= 400:
                    return HealthResult(status="unhealthy", latency_ms=latency_ms, error_message=f"HTTP {resp.status_code}: {resp.text[:200]}")
                from server.config import get_config
                threshold = get_config().health_check.healthy_latency_threshold_ms
                return HealthResult(status="healthy" if latency_ms < threshold else "degraded", latency_ms=latency_ms, error_message="")
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            return HealthResult(status="unhealthy", latency_ms=latency_ms, error_message=str(e)[:200])
