"""
OpenAI 兼容格式适配器
适用于绝大多数服务商：OpenAI, DeepSeek, Groq, 通义千问, 智谱, 等
"""
import time
import json
import httpx
from typing import AsyncGenerator, List
from dataclasses import dataclass
from .base_adapter import BaseAdapter, ModelInfo, HealthResult
from server.schemas.chat import ChatCompletionRequest, ChatCompletionResponse
class OpenAICompatAdapter(BaseAdapter):
    """OpenAI 兼容格式适配器"""
    def __init__(self, timeout: int = 60):
        self.timeout = timeout
    def _build_url(self, base_url: str) -> str:
        """构建 chat completions URL"""
        base = base_url.rstrip('/')
        # 智谱 BigModel: /api/paas/v4/chat/completions（无 /v1）
        if '/api/paas/' in base:
            return f"{base}/chat/completions"
        if not base.endswith('/v1'):
            base += '/v1'
        return f"{base}/chat/completions"
    def _build_models_url(self, base_url: str) -> str:
        base = base_url.rstrip('/')
        if '/api/paas/' in base:
            return f"{base}/models"
        if not base.endswith('/v1'):
            base += '/v1'
        return f"{base}/models"
    def _get_headers(self, api_key: str, extra_headers: dict = None) -> dict:
        headers = {
            "Authorization": f"Bearer {api_key}",
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
        url = self._build_url(base_url)
        headers = self._get_headers(api_key, extra_headers)
        payload = request.model_dump(exclude_none=True)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                body = resp.text[:500]
                raise httpx.HTTPStatusError(
                    f"Client error '{resp.status_code} {resp.reason_phrase}' for url '{url}'\nResponse: {body}",
                    request=resp.request, response=resp
                )
            return resp.json()
    async def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> AsyncGenerator[dict, None]:
        url = self._build_url(base_url)
        headers = self._get_headers(api_key, extra_headers)
        payload = request.model_dump(exclude_none=True)
        timeout_error = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream('POST', url, headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode('utf-8', errors='replace')
                        raise httpx.HTTPStatusError(
                            f"Client error '{resp.status_code} {resp.reason_phrase}' for url '{url}'\nResponse: {body[:500]}",
                            request=resp.request, response=resp
                        )
                    try:
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith('data: '):
                                line = line[6:]
                            if line == '[DONE]':
                                break
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError:
                                continue
                    except (httpx.ReadTimeout, httpx.ReadError):
                        # 捕获超时/读取错误，先让 async for 和 resp 正常清理完毕，
                        # 再重新抛出，避免 aclose() 竞态
                        timeout_error = httpx.ReadTimeout(
                            f"stream read timeout for '{url}'",
                            request=resp.request, response=resp
                        )
        except Exception:
            # 其他异常直接传播
            raise
        if timeout_error:
            raise timeout_error
    async def list_models(
        self,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> List[ModelInfo]:
        url = self._build_models_url(base_url)
        headers = self._get_headers(api_key, extra_headers)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            models = []
            for item in data.get('data', []):
                model_id = item.get('id', '')
                if not model_id:
                    continue
                is_free = 'free' in model_id.lower()
                models.append(ModelInfo(
                    model_id=model_id,
                    display_name=model_id,
                    is_free=is_free,
                    input_price=0.0,
                    output_price=0.0,
                    supports_streaming=True,
                    context_length=4096
                ))
            return models
    async def health_check(
        self,
        model: str,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
        timeout: int = 10
    ) -> HealthResult:
        url = self._build_url(base_url)
        headers = self._get_headers(api_key, extra_headers)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False
        }
        start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
                latency_ms = (time.time() - start_time) * 1000
                if resp.status_code == 429:
                    return HealthResult(
                        status="rate_limited",
                        latency_ms=latency_ms,
                        error_message="Rate limit exceeded"
                    )
                resp.raise_for_status()
                # 成功了，根据延迟判断状态
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
                error_message=f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            return HealthResult(
                status="unhealthy",
                latency_ms=latency_ms,
                error_message=str(e)[:200]
            )