"""
OpenAI 兼容格式适配器
适用于绝大多数服务商：OpenAI, DeepSeek, Groq, 通义千问, 智谱, 等
"""
import time
import json
import uuid
import logging
import httpx
from typing import AsyncGenerator, List
from dataclasses import dataclass
from .base_adapter import BaseAdapter, ModelInfo, HealthResult
from server.schemas.chat import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)


def _proxy_kwargs() -> dict:
    """从代理池取 httpx 代理参数；代理池关闭时返回空 dict（即直连）"""
    from server.core.proxy_pool import get_proxy_pool
    return get_proxy_pool().proxied_kwargs()


def _is_local_url(base_url: str) -> bool:
    """判断目标是否为本地环回地址（127.0.0.1 / localhost / ::1 / *.local）。
    本地服务（如 AtomCode2API）直连即可，绕过 socks5 代理，否则环回地址会被
    发往代理而连接失败（ConnectError: All connection attempts failed）。"""
    if not base_url:
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url).hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1") or host.endswith(".local")
    except Exception:
        return False


def _ensure_tool_call_ids(messages):
    """
    防御性修复：严格上游（如 烁 / sensenova 的代理）要求
      1) 每条 assistant 的 tool_calls 必须携带非空 id；
      2) 每条 role:'tool' 消息的 tool_call_id 必须引用一个真实存在的 assistant tool_call id。
    否则直接 400（`missing/invalid tool_call_id`）。

    部分客户端（OpenClaw / Codex 等）会让 assistant tool_call 的 id 为 null 或空串，
    model_dump(exclude_none=True) 会剔除 null、但保留空串 —— 两种都会触发上游报错。

    修复策略（只补不删，符合 OpenAI 规范）：
      - 先给每条 assistant tool_call 补空/缺的 id（空串或 None → 合成 call_<uuid>）；
      - 再按出现顺序把 tool 消息依次配对到 assistant 的 tool_call id（保证引用一致）。
    """
    if not messages:
        return messages
    repaired = 0
    # Pass 1：保证每条 assistant tool_call 有非空 id，同时收集有序 id 列表
    asst_ids = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict):
                    if not tc.get("id"):
                        tc["id"] = f"call_{uuid.uuid4().hex}"
                        repaired += 1
                    asst_ids.append(tc["id"])
    # Pass 2：tool 消息按顺序配对到 assistant 的 tool_call id
    ptr = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            if ptr < len(asst_ids):
                want = asst_ids[ptr]
                if m.get("tool_call_id") != want:
                    m["tool_call_id"] = want
                    repaired += 1
                ptr += 1
            else:
                # 孤儿 tool 消息（前方无对应 assistant tool_call）——给一个占位 id 避免空值
                m["tool_call_id"] = f"call_{uuid.uuid4().hex}"
                repaired += 1
    if repaired:
        logger.warning(
            "转发上游前已自动修复 %d 个 tool_call id（避免上游因空 id 报 400）",
            repaired,
        )
    return messages


class OpenAICompatAdapter(BaseAdapter):
    """OpenAI 兼容格式适配器"""
    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self.last_proxy_url = None

    def _proxy(self, base_url: str = None) -> dict:
        """取代理参数并记下本次线请求实际使用的代理 URL（写入 ContextVar，供日志落库）。
        本地目标(127.0.0.1/localhost/::1)绕过代理，避免环回地址被发往 socks5 代理而连接失败。"""
        if _is_local_url(base_url):
            self.last_proxy_url = None
            from server.core.proxy_pool import CURRENT_PROXY_URL
            CURRENT_PROXY_URL.set(None)
            return {}
        pk = _proxy_kwargs()
        url = pk.get("proxy")
        self.last_proxy_url = url
        from server.core.proxy_pool import CURRENT_PROXY_URL
        CURRENT_PROXY_URL.set(url)
        return pk
    def _build_url(self, base_url: str) -> str:
        """构建 chat completions URL"""
        base = base_url.rstrip('/')
        # 智谱 BigModel: /api/paas/v4/chat/completions（无 /v1）
        if '/api/paas/' in base:
            return f"{base}/chat/completions"
        # 防御性兜底：base_url 已经包含完整 chat 路径时直接返回
        # 修复 MiMo Code Free 等免登录 provider 因路由 bug 误经 adapter 被 URL 二次追加为
        # .../openai/chat/v1/chat/completions 的问题
        # （free_tier 正常路径应走 server/core/free_providers.py，不走此 adapter）
        if base.endswith('/chat/completions') or base.endswith('/openai/chat'):
            return base
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
        # v3.1：free_tier / OAuth 路径可能给空字符串 — 不带 Authorization 头
        # httpx 会因 "Bearer " 尾随空格抛 LocalProtocolError
        if api_key and str(api_key).strip():
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
        else:
            # 无密钥请求（部分本地/免费端点接受匿名调用）
            headers = {"Content-Type": "application/json"}
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
        payload["messages"] = _ensure_tool_call_ids(payload.get("messages") or [])
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy(base_url)) as client:
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
        payload["messages"] = _ensure_tool_call_ids(payload.get("messages") or [])
        timeout_error = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout, **self._proxy(base_url)) as client:
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
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy(base_url)) as client:
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
            async with httpx.AsyncClient(timeout=timeout, **self._proxy(base_url)) as client:
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