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
from server.core.model_capabilities import infer_reasoning_effort_support
from server.schemas.chat import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)


def _proxy_kwargs(*, force: bool = False) -> dict:
    """从代理池取 httpx 代理参数；代理池关闭时返回空 dict（即直连）"""
    from server.core.proxy_pool import get_proxy_pool
    return get_proxy_pool().proxied_kwargs(force=force)


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
    def __init__(self, timeout: int = 180):
        self.timeout = timeout
        self.last_proxy_url = None

    def _proxy(self, base_url: str = None, force: bool = False) -> dict:
        """取代理参数并记下本次线请求实际使用的代理 URL（写入 ContextVar，供日志落库）。
        本地目标(127.0.0.1/localhost/::1)绕过代理，避免环回地址被发往 socks5 代理而连接失败。"""
        if _is_local_url(base_url):
            self.last_proxy_url = None
            from server.core.proxy_pool import CURRENT_PROXY_URL
            CURRENT_PROXY_URL.set(None)
            return {}
        pk = _proxy_kwargs(force=force)
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
        # Proxy metadata is routing state and must never become an upstream header.
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
        force_proxy = bool((extra_headers or {}).get("__proxy_force"))
        payload = request.model_dump(exclude_none=True)
        # reasoning dict 是网关内部思考控制提示（anthropic 出站方言）；OpenAI 兼容上游
        # 只认 reasoning_effort，透传非标字段有被严格上游 400 的风险
        payload.pop("reasoning", None)
        payload["messages"] = _ensure_tool_call_ids(payload.get("messages") or [])
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy(base_url, force_proxy)) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                body = resp.text[:500]
                raise httpx.HTTPStatusError(
                    f"Client error '{resp.status_code} {resp.reason_phrase}' for url '{url}'\nResponse: {body}",
                    request=resp.request, response=resp
                )
            data = resp.json()
            try:
                from server.config import get_config
                if get_config().adapters.openai_compat.reasoning == "drop":
                    for _ch in (data.get("choices") or []):
                        (_ch.get("message") or {}).pop("reasoning_content", None)
            except Exception:
                pass
            return data
    async def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None
    ) -> AsyncGenerator[dict, None]:
        url = self._build_url(base_url)
        headers = self._get_headers(api_key, extra_headers)
        force_proxy = bool((extra_headers or {}).get("__proxy_force"))
        payload = request.model_dump(exclude_none=True)
        payload.pop("reasoning", None)  # 内部思考控制提示，非 OpenAI 标准字段
        payload["messages"] = _ensure_tool_call_ids(payload.get("messages") or [])
        timeout_error = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout, **self._proxy(base_url, force_proxy)) as client:
                async with client.stream('POST', url, headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode('utf-8', errors='replace')
                        raise httpx.HTTPStatusError(
                            f"Client error '{resp.status_code} {resp.reason_phrase}' for url '{url}'\nResponse: {body[:500]}",
                            request=resp.request, response=resp
                        )
                    try:
                        from server.config import get_config
                        _acfg = get_config().adapters.openai_compat
                        _drop_reasoning = (_acfg.reasoning == "drop")
                        _chunk_size = max(1, int(_acfg.content_chunk_size))
                        async def _parse_lines():
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
                        async for c in _consolidate_openai_stream(_parse_lines(), _drop_reasoning, _chunk_size):
                            yield c
                    except (httpx.ReadTimeout, httpx.ReadError) as _te:
                        # 捕获超时/读取错误，先让 async for 和 resp 正常清理完毕，
                        # 再重新抛出，避免 aclose() 竞态。
                        # 注意：httpx 0.28 的 ReadTimeout/ReadError 等传输类异常
                        # __init__ 只接受 (message, *, request)，不接受 response=，
                        # 给它传 response= 会抛 TypeError: RequestError.__init__()
                        # got an unexpected keyword argument 'response'（掩盖真实超时）。
                        # 直接复用原始异常对象即可，避免重新构造。
                        timeout_error = _te
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
        force_proxy = bool((extra_headers or {}).get("__proxy_force"))
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy(base_url, force_proxy)) as client:
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
                    context_length=0,
                    supports_reasoning_effort=infer_reasoning_effort_support("openai_compat", model_id)
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
        force_proxy = bool((extra_headers or {}).get("__proxy_force"))
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False
        }
        start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=timeout, **self._proxy(base_url, force_proxy)) as client:
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


async def _consolidate_openai_stream(chunks, drop_reasoning: bool, chunk_size: int):
    """把上游逐字符/逐词的 OpenAI SSE chunk 流合并成较大的 chunk。

    - content 缓冲到 chunk_size 或自然边界再吐，消除“逐字符碎片 / 串行空格”
    - reasoning_content 缓冲并前置 flush（思考在前）；drop_reasoning=True 时丢弃
    - role / tool_calls / usage / finish_reason 均原样即时透传，不被缓冲
    """
    last_meta = {}
    st = {"sent_role": False, "content": "", "reasoning": "", "reasoning_flushed": False}
    out = []

    def mk(delta, src=None, finish=None):
        m = src if isinstance(src, dict) else last_meta
        return {
            "id": m.get("id"),
            "object": "chat.completion.chunk",
            "created": m.get("created"),
            "model": m.get("model"),
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def flush_reasoning(src):
        if st["reasoning"]:
            out.append(mk({"reasoning_content": st["reasoning"]}, src))
            st["reasoning"] = ""
            st["reasoning_flushed"] = True

    def flush_content(src):
        if st["content"]:
            out.append(mk({"content": st["content"]}, src))
            st["content"] = ""

    async for c in chunks:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("error"), dict) or (c.get("error") and "choices" not in c):
            # 错误块：先 flush 已缓冲内容，再原样透传
            flush_reasoning(c)
            flush_content(c)
            for oc in out:
                yield oc
            out.clear()
            yield c
            continue
        last_meta = {k: c.get(k) for k in ("id", "created", "model")}
        choices = c.get("choices") or []
        if not choices:
            # 顶层字段（如独立 usage）原样透传
            yield c
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or {}
        finish = choice.get("finish_reason")
        tc = delta.get("tool_calls")
        role = delta.get("role")
        reasoning = delta.get("reasoning_content")
        content = delta.get("content")
        # role 只发一次
        if role and not st["sent_role"]:
            out.append(mk({"role": role}, c))
            st["sent_role"] = True
        # tool_calls 必须立即透传，不能缓冲
        if tc is not None:
            flush_reasoning(c)
            flush_content(c)
            for oc in out:
                yield oc
            out.clear()
            yield c
            continue
        # reasoning 缓冲（可配置丢弃）
        if reasoning and not drop_reasoning:
            st["reasoning"] += reasoning
        # content 缓冲；内容开始时先把已积累的 thinking 前置 flush
        if content:
            if st["content"] == "" and st["reasoning"] and not st["reasoning_flushed"]:
                flush_reasoning(c)
            st["content"] += content
            if len(st["content"]) >= chunk_size:
                flush_content(c)
        # usage 收尾
        if c.get("usage"):
            flush_reasoning(c)
            flush_content(c)
            for oc in out:
                yield oc
            out.clear()
            yield c
            continue
        # finish_reason 收尾
        if finish:
            flush_reasoning(c)
            flush_content(c)
            out.append(mk({}, c, finish=finish))
            for oc in out:
                yield oc
            out.clear()
            continue
        # 普通缓冲：把累积的发出去
        for oc in out:
            yield oc
        out.clear()
    # 流结束：flush 残余
    if st["reasoning"] or st["content"]:
        flush_reasoning(None)
        flush_content(None)
        for oc in out:
            yield oc
        out.clear()
