"""
GitHub Models 适配器
GitHub Models API 是 OpenAI 兼容格式，但认证方式不同：
- 使用 GitHub Personal Access Token
- Authorization header: "Bearer ghp_xxx"
- Base URL: https://models.inference.ai.azure.com
"""
import time
import json
import httpx


def _proxy_kwargs(*, force: bool = False) -> dict:
    """从代理池取 httpx 代理参数；代理池关闭时返回空 dict（即直连）"""
    from server.core.proxy_pool import get_proxy_pool
    return get_proxy_pool().proxied_kwargs(force=force)


from typing import AsyncGenerator, List
from .base_adapter import BaseAdapter, ModelInfo, HealthResult
from server.core.model_capabilities import infer_reasoning_effort_support
from server.schemas.chat import ChatCompletionRequest, ChatCompletionResponse
# GitHub Models 内置价格表 (美元 / 百万 tokens)
GITHUB_PRICING = {
    # GPT 系列 (通过 GitHub)
    "gpt-4o": {"input": 5.0, "output": 15.0, "is_free": False},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6, "is_free": False},
    # Llama 系列
    "meta-llama-3.1-405b-instruct": {"input": 0, "output": 0, "is_free": True},
    "meta-llama-3.1-70b-instruct": {"input": 0, "output": 0, "is_free": True},
    "meta-llama-3.1-8b-instruct": {"input": 0, "output": 0, "is_free": True},
    "meta-llama-3-70b-instruct": {"input": 0, "output": 0, "is_free": True},
    "meta-llama-3-8b-instruct": {"input": 0, "output": 0, "is_free": True},
    # Mistral 系列
    "mistral-large": {"input": 0, "output": 0, "is_free": True},
    "mistral-small": {"input": 0, "output": 0, "is_free": True},
    "mistral-nemo": {"input": 0, "output": 0, "is_free": True},
    "ministral-3b": {"input": 0, "output": 0, "is_free": True},
    # Phi 系列
    "phi-3.5-mini-instruct": {"input": 0, "output": 0, "is_free": True},
    "phi-3.5-moe-instruct": {"input": 0, "output": 0, "is_free": True},
    "phi-3-mini-instruct": {"input": 0, "output": 0, "is_free": True},
    "phi-3-small-instruct": {"input": 0, "output": 0, "is_free": True},
    # Cohere
    "cohere-command-r": {"input": 0, "output": 0, "is_free": True},
    "cohere-command-r-plus": {"input": 0, "output": 0, "is_free": True},
    # AI21
    "ai21-jamba-1.5-large": {"input": 0, "output": 0, "is_free": True},
    "ai21-jamba-1.5-mini": {"input": 0, "output": 0, "is_free": True},
    # OpenAI (通过 GitHub)
    "openai-whisper-large-v3-turbo": {"input": 0, "output": 0, "is_free": True},
}
class GitHubAdapter(BaseAdapter):
    """GitHub Models 适配器 (OpenAI 兼容 + GitHub Token 认证)"""
    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self.last_proxy_url = None

    def _proxy(self, force: bool = False) -> dict:
        """取代理参数并记下本次线请求实际使用的代理 URL（写入 ContextVar，供日志落库）。"""
        pk = _proxy_kwargs(force=force)
        url = pk.get("proxy")
        self.last_proxy_url = url
        from server.core.proxy_pool import CURRENT_PROXY_URL
        CURRENT_PROXY_URL.set(url)
        return pk
    def _build_url(self, base_url: str) -> str:
        """构建 chat completions URL"""
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        return f"{base}/chat/completions"
    def _build_models_url(self, base_url: str) -> str:
        """构建 models list URL"""
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        return f"{base}/models"
    def _get_headers(self, api_key: str, extra_headers: dict = None) -> dict:
        """GitHub 使用 Bearer token 认证"""
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update({k: v for k, v in extra_headers.items() if k not in ("__proxy_force", "__proxy_url")})
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
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy(bool((extra_headers or {}).get("__proxy_force")))) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
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
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy(bool((extra_headers or {}).get("__proxy_force")))) as client:
            async with client.stream('POST', url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
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
    async def list_models(
        self,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> List[ModelInfo]:
        """从 GitHub Models API 获取模型列表"""
        url = self._build_models_url(base_url)
        headers = self._get_headers(api_key, extra_headers)
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy(bool((extra_headers or {}).get("__proxy_force")))) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            models = []
            for item in data.get('data', []):
                model_id = item.get('id', '')
                if not model_id:
                    continue
                # 匹配内置定价表
                pricing = GITHUB_PRICING.get(model_id)
                if pricing:
                    is_free = pricing["is_free"]
                    input_price = pricing["input"]
                    output_price = pricing["output"]
                else:
                    # 未知模型：根据 ID 特征判断
                    is_free = (
                        'free' in model_id.lower() or
                        'llama' in model_id.lower() or
                        'mistral' in model_id.lower() or
                        'phi' in model_id.lower() or
                        'jamba' in model_id.lower() or
                        'command-r' in model_id.lower() or
                        'whisper' in model_id.lower()
                    )
                    input_price = 0.0
                    output_price = 0.0
                models.append(ModelInfo(
                    model_id=model_id,
                    display_name=model_id,
                    is_free=is_free,
                    input_price=input_price,
                    output_price=output_price,
                    supports_streaming=True,
                    context_length=0,
                    supports_reasoning_effort=infer_reasoning_effort_support("github", model_id)
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
            async with httpx.AsyncClient(timeout=timeout, **self._proxy(bool((extra_headers or {}).get("__proxy_force")))) as client:
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
                error_message=f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            return HealthResult(
                status="unhealthy",
                latency_ms=latency_ms,
                error_message=str(e)[:200]
            )
