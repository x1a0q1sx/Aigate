"""
Anthropic Claude API 格式适配器
转换为 OpenAI 兼容输出格式
"""
import time
import json
import httpx
from typing import AsyncGenerator, List
from dataclasses import dataclass
from .base_adapter import BaseAdapter, ModelInfo, HealthResult
from server.schemas.chat import ChatCompletionRequest, ChatCompletionResponse
class AnthropicAdapter(BaseAdapter):
    """Anthropic Messages API 适配器"""
    def __init__(self, timeout: int = 60):
        self.timeout = timeout
    def _convert_messages(self, request: ChatCompletionRequest) -> List[dict]:
        """转换 OpenAI messages 到 Anthropic 格式"""
        messages = []
        for msg in request.messages:
            messages.append({
                "role": msg.role,
                "content": msg.content
            })
        return messages
    def _get_headers(self, api_key: str, extra_headers: dict = None) -> dict:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }
        if extra_headers:
            headers.update(extra_headers)
        return headers
    async def chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> ChatCompletionResponse:
        base = base_url.rstrip('/')
        url = f"{base}/v1/messages"
        headers = self._get_headers(api_key, extra_headers)
        payload = {
            "model": request.model,
            "messages": self._convert_messages(request),
            "max_tokens": request.max_tokens or 1024,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        import time
        created = int(time.time())
        async with httpx.AsyncClient(timeout=self.timeout * 1000) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        # 转换为 OpenAI 格式
        content = data.get("content", [{}])[0].get("text", "")
        input_tokens = data.get("usage", {}).get("input_tokens", 0)
        output_tokens = data.get("usage", {}).get("output_tokens", 0)
        return {
            "id": data.get("id", f"anthropic-{created}"),
            "object": "chat.completion",
            "created": created,
            "model": f"anthropic/{request.model}",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens
            }
        }
    async def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> AsyncGenerator[dict, None]:
        base = base_url.rstrip('/')
        url = f"{base}/v1/messages"
        headers = self._get_headers(api_key, extra_headers)
        payload = {
            "model": request.model,
            "messages": self._convert_messages(request),
            "max_tokens": request.max_tokens or 1024,
            "betas": ["increased-context-length-1m"],
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        import time
        created = int(time.time())
        chunk_id = f"anthropic-{created}"
        async with httpx.AsyncClient(timeout=self.timeout * 1000) as client:
            async with client.stream('POST', url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith('data: '):
                        continue
                    line = line[6:]
                    if line == '[DONE]':
                        break
                    try:
                        data = json.loads(line)
                        typ = data.get("type")
                        if typ == "content_block_delta":
                            delta = data.get("delta", {})
                            text = delta.get("text", "")
                            if text:
                                yield {
                                    "id": chunk_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": f"anthropic/{request.model}",
                                    "choices": [{
                                        "delta": {"content": text},
                                        "index": 0
                                    }]
                                }
                    except json.JSONDecodeError:
                        continue
    async def list_models(
        self,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> List[ModelInfo]:
        """Anthropic 没有公开 list models API，返回已知模型列表"""
        return [
            ModelInfo(model_id="claude-3-opus-20240229", display_name="Claude 3 Opus",
                      input_price=15.0, output_price=75.0, is_free=False),
            ModelInfo(model_id="claude-3-sonnet-20240229", display_name="Claude 3 Sonnet",
                      input_price=3.0, output_price=15.0, is_free=False),
            ModelInfo(model_id="claude-3-haiku-20240307", display_name="Claude 3 Haiku",
                      input_price=0.25, output_price=1.25, is_free=False),
            ModelInfo(model_id="claude-2.1", display_name="Claude 2.1",
                      input_price=8.0, output_price=24.0, is_free=False),
        ]
    async def health_check(
        self,
        model: str,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
        timeout: int = 10
    ) -> HealthResult:
        base = base_url.rstrip('/')
        url = f"{base}/v1/messages"
        headers = self._get_headers(api_key, extra_headers)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1
        }
        start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=timeout * 1000) as client:
                resp = await client.post(url, headers=headers, json=payload)
                latency_ms = (time.time() - start_time) * 1000
                if resp.status_code == 429:
                    return HealthResult(
                        status="rate_limited",
                        latency_ms=latency_ms,
                        error_message="Rate limit exceeded"
                    )
                resp.raise_for_status()
                from server.config import get_config
                config = get_config()
                threshold = config.health_check.healthy_latency_threshold_ms
                if latency_ms < threshold:
                    status = "healthy"
                else:
                    status = "degraded"
                return HealthResult(
                    status=status,
                    latency_ms=latency_ms,
                    error_message=""
                )
        except httpx.HTTPStatusError as e:
            latency_ms = (time.time() - start_time) * 1000
            return HealthResult(
                status="unhealthy",
                latency_ms=latency_ms,
                error_message=f"HTTP {e.response.status_code}"
            )
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            return HealthResult(
                status="unhealthy",
                latency_ms=latency_ms,
                error_message=str(e)[:200]
            )