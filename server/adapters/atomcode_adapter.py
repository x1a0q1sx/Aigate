"""
AtomCode (AtomGit) 适配器 —— 本地 daemon 签名代理模式

不再由 AIGate 自己实现上游签名（上游签名算法闭源且随版本变化；二进制使用 rustls
自带根证书，无法在本机注入 CA 抓包反推）。改为由 AIGate 自己拉起并管理本机
`atomcode` 可执行文件（daemon 模式）作为签名代理：daemon 直连真实网关
llm-api.atomgit.com 并透明完成鉴权 / 签名，AIGate 仅负责 OpenAI 格式 ↔ daemon
/chat 协议的转换与回传。

协议映射：
  OpenAI /v1/chat/completions  →  daemon POST /chat  {message, stream, model}
  daemon SSE(reasoning/text/tokens/done)  →  OpenAI chat.completion.chunk

注意：daemon 的 /chat 需要一个 `message` 字符串（agent 模式），不传 working_dir
即为纯对话。本适配器把整段多轮 messages 拼成单个 message 字符串转发，保持无状态。
"""
import json
import time
import uuid
import logging
from typing import AsyncGenerator, List, Optional

from .base_adapter import BaseAdapter, ModelInfo, HealthResult
from server.schemas.chat import ChatCompletionRequest
from .atomcode_daemon import get_daemon_client, AtomCodeDaemonError

logger = logging.getLogger(__name__)

_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant"}


# ---------------------------------------------------------------------------
# OpenAI 消息 → daemon message 字符串
# ---------------------------------------------------------------------------
def _extract_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                # image_url 等在本代理中不传递
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return str(content)


def _to_message(request: ChatCompletionRequest) -> str:
    parts = []
    for m in request.messages:
        role = m.role
        text = _extract_text(m.content)
        if role == "tool":
            parts.append(f"Tool result ({m.tool_call_id or ''}): {text}")
            continue
        label = _ROLE_LABELS.get(role, role.capitalize() if role else "User")
        parts.append(f"{label}: {text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# OpenAI 格式构件
# ---------------------------------------------------------------------------
def _chunk(chunk_id: str, model: str, created: int, content=None,
           reasoning=None, finish=None, usage=None) -> dict:
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _to_usage(ev) -> Optional[dict]:
    if ev is None:
        return None
    if isinstance(ev, dict):
        return {
            "prompt_tokens": int(ev.get("prompt", 0) or 0),
            "completion_tokens": int(ev.get("completion", 0) or 0),
            "total_tokens": int(ev.get("total", 0) or 0),
        }
    if isinstance(ev, int):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": ev}
    return None


# ---------------------------------------------------------------------------
# model(内层名) → provider 键 映射缓存
# daemon 的 /chat 用 `provider` 字段（provider 键名）选模型，忽略 `model` 字段；
# 故需把 AIGate 传来的裸 model_id 反查成 provider 键再下发。
# ---------------------------------------------------------------------------
_PROVIDER_MAP: dict = {}
_PROVIDER_MAP_TS: float = 0.0
_MAP_TTL: int = 300


async def _resolve_provider(client, model: str) -> Optional[str]:
    """把内层模型名（如 deepseek-v4-flash）解析成 daemon 的 provider 键。

    返回 None 表示不指定（daemon 用 default_provider）。
    """
    global _PROVIDER_MAP, _PROVIDER_MAP_TS
    if not model or model == "auto":
        return None
    now = time.time()
    if now - _PROVIDER_MAP_TS > _MAP_TTL or model not in _PROVIDER_MAP:
        try:
            raw = await client.list_models()
            _PROVIDER_MAP = {
                m.get("model"): m.get("provider")
                for m in raw
                if m.get("model")
            }
            _PROVIDER_MAP_TS = now
        except Exception:
            return _PROVIDER_MAP.get(model)
    return _PROVIDER_MAP.get(model)


def _payload(request: ChatCompletionRequest, provider: str = None) -> dict:
    payload = {"message": _to_message(request), "stream": True}
    model = (request.model or "").strip()
    if model and model != "auto":
        payload["model"] = model
    # daemon 用 provider 键选模型，必须下发
    if provider:
        payload["provider"] = provider
    return payload


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------
class AtomCodeAdapter(BaseAdapter):
    """AtomCode / AtomGit 适配器（本地 daemon 签名代理）。

    AIGate 负责把 OpenAI 请求转成 daemon 的 /chat 协议并回传；签名 / 鉴权由本地
    atomcode daemon 完成。AIGate 自己拉起并管理 daemon 生命周期（复用已运行的实例）。
    """

    def __init__(self, timeout: int = 120):
        self.timeout = timeout

    # ---- 流式 ----
    async def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
    ) -> AsyncGenerator[dict, None]:
        client = await get_daemon_client()
        provider = await _resolve_provider(client, request.model or "")
        payload = _payload(request, provider)
        model = request.model or "atomcode"
        created = int(time.time())
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
        usage = None
        finished = False
        try:
            async for ev in client.stream_chat(payload):
                t = ev.get("type")
                if t == "reasoning":
                    yield _chunk(chunk_id, model, created, reasoning=ev.get("content", ""))
                elif t == "text":
                    yield _chunk(chunk_id, model, created, content=ev.get("content", ""))
                elif t == "tokens":
                    usage = ev
                elif t == "done":
                    if usage is None:
                        usage = ev.get("tokens")
                    yield _chunk(chunk_id, model, created, finish="stop",
                                 usage=_to_usage(usage))
                    finished = True
                    return
                elif t == "error":
                    raise AtomCodeDaemonError(ev.get("message", "daemon error"))
                elif t == "stopped":
                    if not finished:
                        yield _chunk(chunk_id, model, created, finish="stop",
                                     usage=_to_usage(usage))
                        finished = True
                        return
        except AtomCodeDaemonError:
            raise
        except Exception as e:  # 网络/解析等
            logger.error("[atomcode] 流式请求失败: %s", e)
            raise
        if not finished:
            yield _chunk(chunk_id, model, created, finish="stop", usage=_to_usage(usage))

    # ---- 非流式 ----
    async def chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
    ) -> dict:
        client = await get_daemon_client()
        provider = await _resolve_provider(client, request.model or "")
        payload = _payload(request, provider)
        model = request.model or "atomcode"
        created = int(time.time())
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        usage = None
        async for ev in client.stream_chat(payload):
            t = ev.get("type")
            if t == "text":
                text_parts.append(ev.get("content", ""))
            elif t == "reasoning":
                reasoning_parts.append(ev.get("content", ""))
            elif t == "tokens":
                usage = ev
            elif t == "done":
                if usage is None:
                    usage = ev.get("tokens")
                break
            elif t == "error":
                raise AtomCodeDaemonError(ev.get("message", "daemon error"))

        message = {"role": "assistant", "content": "".join(text_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "stop",
                }
            ],
            "usage": _to_usage(usage) or {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0
            },
        }

    # ---- 模型列表 ----
    async def list_models(
        self,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
    ) -> List[ModelInfo]:
        client = await get_daemon_client()
        raw = await client.list_models()
        models: List[ModelInfo] = []
        for m in raw:
            name = m.get("model") or m.get("id")
            if not name:
                continue
            prov = m.get("provider", "")
            display = f"{prov}/{name}" if prov else name
            models.append(
                ModelInfo(
                    model_id=name,
                    display_name=display,
                    supports_streaming=True,
                    context_length=int(m.get("context_window", 64000) or 64000),
                )
            )
        return models

    # ---- 健康探测 ----
    async def health_check(
        self,
        model: str,
        api_key: str,
        base_url: str,
        extra_headers: dict = None,
        timeout: int = 10,
    ) -> HealthResult:
        start = time.time()
        try:
            client = await get_daemon_client()
            await client.health()
            latency_ms = (time.time() - start) * 1000
            return HealthResult(status="healthy", latency_ms=latency_ms, error_message="")
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            return HealthResult(
                status="unhealthy", latency_ms=latency_ms, error_message=str(e)[:200]
            )
