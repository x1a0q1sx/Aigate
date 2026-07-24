"""
Free Provider 专用 executor — 给「无需密钥直接登录」的 9Router free tier provider 用。

支持两类：
- opencode:  endpoint POST https://opencode.ai/zen/v1/chat/completions
             header: Authorization: Bearer public + x-opencode-client: desktop
- mimo-free: endpoint POST https://api.xiaomimimo.com/api/free-ai/openai/chat
             先 bootstrap 拿 JWT，再带 Authorization: Bearer <jwt> + x-session-affinity + 系统 prompt 必含 MiMoCode 标记防 403

为什么不在 OpenAICompatAdapter 里塞这些分支：因为这是「provider 特有协议」，openai_compat 适配器要保持纯净。
所以这里另起一个 LightweightFreeAdapter，根据 provider 的 noAuth 标记选路径。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import random
import socket
import time
from typing import AsyncGenerator, Dict, List, Optional

import httpx


def _proxy_kwargs() -> dict:
    """从代理池取 httpx 代理参数；代理池关闭时返回空 dict（即直连）"""
    from server.core.proxy_pool import get_proxy_pool
    return get_proxy_pool().proxied_kwargs()


from server.schemas.chat import ChatCompletionRequest


# ── 各 free provider 的元数据（照搬 9router registry）────────────────
_FREE_PROVIDERS_META: Dict[str, dict] = {
    "opencode": {
        "name": "OpenCode Free",
        "base_url": "https://opencode.ai",
        "chat_path": "/zen/v1/chat/completions",
        "auth_mode": "static_token",
        "static_token": "public",
        "extra_headers": {"x-opencode-client": "desktop", "Accept": "text/event-stream"},
        # 2026-07-08 实测：`Bearer public` + `x-opencode-client: desktop` 正常，/zen/v1/models 返回 51 个模型
        # 注意：OpenCode 免费层只支持具体模型名（如 claude-sonnet-4 / gpt-5.5 / gemini-3-flash），
        # 不存在 `opencode-auto` 这类聚合名，传错模型名会上游 401 报 "Model xxx is not supported"
        "upstream_might_fail": False,
    },
    "mimo-free": {
        "name": "MiMo Code Free",
        "base_url": "https://api.xiaomimimo.com",
        "chat_path": "/api/free-ai/openai/chat",
        "auth_mode": "bootstrap_jwt",
        "bootstrap_url": "https://api.xiaomimimo.com/api/free-ai/bootstrap",
        "session_affinity_prefix": "ses_",
        "session_id_length": 24,
        "system_marker": "You are MiMoCode, an interactive CLI tool that helps users with software engineering tasks.",
        # 2026-07-08 实测：免费层无 /models 接口（404）。9Router 注册表 mimo-free.js 仅硬编码 `mimo-auto` 一个模型，
        # 且 `passthroughModels: true` + modelsFetcher 动态拉取，但上游只认 `mimo-auto`（其他名一律 "Unsupported model"）。
        # 故 MiMo Code Free 真实支持的模型就只有 `mimo-auto`（已写入 DB model 301，auto_enabled=1）。
        # 注意：曾手填的 `gpt-free`(model 299) 实测 Unsupported model，已禁用。
        "upstream_might_fail": False,
    },
}


def list_free_provider_codes() -> List[str]:
    return list(_FREE_PROVIDERS_META.keys())


def get_free_provider_meta(code: str) -> Optional[dict]:
    return _FREE_PROVIDERS_META.get(code)


def resolve_free_code(provider_name: str, oauth_code: Optional[str] = None) -> Optional[str]:
    """根据 provider 的 oauth_code 或 name 解析到 free executor 的 code 键。

    优先级:
    1. oauth_code 不为空 → 直接用它当 key（精确匹配）
    2. provider.name 直接匹配 _FREE_PROVIDERS_META 的 key（如 'opencode', 'mimo-free'）
    3. provider.name 对 meta 的 display name 做模糊匹配（如 'MiMo Code Free' → 'mimo-free'）
    4. 都匹配不上 → 返回 None（调用方报错）
    """
    code = (oauth_code or "").strip()
    if code and code in _FREE_PROVIDERS_META:
        return code
    # provider.name 直接命中 key
    name = provider_name.strip()
    if name in _FREE_PROVIDERS_META:
        return name
    # 模糊匹配 display name（大小写不敏感，忽略空格差异）
    name_key = name.lower().replace(" ", "")
    for key, meta in _FREE_PROVIDERS_META.items():
        display = meta.get("name", "").lower().replace(" ", "")
        if display and display == name_key:
            return key
    return None


USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

SESSION_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


def _device_fingerprint() -> str:
    """和 9Router MiMo 一致的本机指纹"""
    try:
        username = os.getlogin() or "unknown-user"
    except Exception:
        username = "unknown-user"
    try:
        cpu = (platform.processor() or "unknown-cpu").strip()
    except Exception:
        cpu = "unknown-cpu"
    try:
        hostname = socket.gethostname() or "unknown-host"
    except Exception:
        hostname = "unknown-host"
    seed = f"{hostname}|{platform.system()}|{platform.machine()}|{cpu}|{username}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _gen_session_id(prefix: str = "ses_", length: int = 24) -> str:
    sid = "".join(random.choice(SESSION_CHARS) for _ in range(length))
    return prefix + sid


def _parse_jwt_exp(jwt: str) -> float:
    import base64
    try:
        payload_b64 = jwt.split(".")[1]
        pad = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad).decode("utf-8"))
        if "exp" in payload:
            return float(payload["exp"]) * 1000
    except Exception:
        pass
    return time.time() * 1000 + 3000 * 1000


# ── MiMo JWT 进程内缓存（重启清空）──
_MIMO_JWT: Dict[str, object] = {"token": None, "expires_ms": 0}
_MIMO_LOCK = asyncio.Lock()


async def _bootstrap_mimo_jwt(client: httpx.AsyncClient, user_agent: Optional[str] = None) -> str:
    if _MIMO_JWT["token"] and time.time() * 1000 < _MIMO_JWT["expires_ms"] - 300 * 1000:
        return _MIMO_JWT["token"]
    async with _MIMO_LOCK:
        if _MIMO_JWT["token"] and time.time() * 1000 < _MIMO_JWT["expires_ms"] - 300 * 1000:
            return _MIMO_JWT["token"]
        resp = await client.post(
            "https://api.xiaomimimo.com/api/free-ai/bootstrap",
            json={"client": _device_fingerprint()},
            headers={
                "Content-Type": "application/json",
                "User-Agent": user_agent or USER_AGENTS[0],
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"mimo bootstrap failed HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not data.get("jwt"):
            raise RuntimeError("mimo bootstrap returned no jwt")
        _MIMO_JWT["token"] = data["jwt"]
        _MIMO_JWT["expires_ms"] = _parse_jwt_exp(data["jwt"])
        return data["jwt"]


def _inject_mimo_marker(payload: dict) -> dict:
    """如果 messages 里没有 MiMoCode marker，就在最前面塞一条 system。"""
    marker = "You are MiMoCode, an interactive CLI tool that helps users with software engineering tasks."
    messages = payload.get("messages") or []
    has = any(m.get("role") == "system" and isinstance(m.get("content"), str) and marker in m["content"]
              for m in messages)
    if has:
        return payload
    return {**payload, "messages": [{"role": "system", "content": marker}, *messages]}


def _surface_free_error(provider_code: str, resp, mode: str):
    """Translate upstream free-provider errors into actionable RuntimeError messages.

    Recognizes MiMo risk-control (code 441) so the UI shows the real cause
    ("upstream risk control") instead of a generic 502 free_provider_failed.
    """
    body = ""
    try:
        body = resp.text or ""
    except Exception:
        pass
    code = None
    err_type = None
    try:
        d = json.loads(body)
        err = d.get("error", d) if isinstance(d, dict) else None
        if isinstance(err, dict):
            code = err.get("code")
            err_type = err.get("type")
    except Exception:
        pass
    if provider_code == "mimo-free" and (code == "441" or err_type == "risk_control"):
        raise RuntimeError(
            "mimo-free risk_control (441): upstream risk control, likely exporter IP banned. try another network or retry later. raw: " + body[:200]
        )
    raise RuntimeError(f"free provider {provider_code} {mode} HTTP {resp.status_code}: " + body[:300])


class FreeProviderExecutor:
    """简化的 free provider 执行器 — 内部用 httpx。

    路径：服务返回 stream SSE（httpx iter_lines）或非流式 json。
    """

    def __init__(self, provider_code: str, timeout: int = 120):
        self.provider_code = provider_code
        self.timeout = timeout
        self.last_proxy_url = None
        self.meta = _FREE_PROVIDERS_META.get(provider_code)
        if not self.meta:
            raise ValueError(f"unknown free provider: {provider_code}")
        # mimo 会话黏性（一个 FreeProviderExecutor 实例一个 session id）
        if provider_code == "mimo-free":
            self._session_id = _gen_session_id(
                self.meta["session_affinity_prefix"],
                self.meta["session_id_length"],
            )
            # Keep a stable UA for the lifetime of this executor/session.
            # Changing UA on every request makes one anonymous JWT/session look like multiple devices.
            self._user_agent = random.choice(USER_AGENTS)
        else:
            self._session_id = None
            self._user_agent = USER_AGENTS[0]

    def _proxy(self) -> dict:
        """取代理参数并记下本次线请求实际使用的代理 URL（写入 ContextVar，供日志落库）。"""
        pk = _proxy_kwargs()
        url = pk.get("proxy")
        self.last_proxy_url = url
        from server.core.proxy_pool import CURRENT_PROXY_URL
        CURRENT_PROXY_URL.set(url)
        return pk

    def _url(self) -> str:
        return self.meta["base_url"] + self.meta["chat_path"]

    async def _build_headers(self, client: httpx.AsyncClient) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.provider_code == "opencode":
            h["Authorization"] = f"Bearer {self.meta['static_token']}"
            h.update(self.meta.get("extra_headers", {}))
        elif self.provider_code == "mimo-free":
            jwt = await _bootstrap_mimo_jwt(client, self._user_agent)
            h.update({
                "Authorization": f"Bearer {jwt}",
                "X-Mimo-Source": "mimocode-cli-free",
                "User-Agent": self._user_agent,
                "x-session-affinity": self._session_id,
            })
            if self.meta.get("Accept") == "text/event-stream":
                h["Accept"] = "text/event-stream"
        return h

    def _prepare_payload(self, request: ChatCompletionRequest, stream: bool) -> dict:
        payload = request.model_dump(exclude_none=True)
        if stream:
            payload["stream"] = True
        # mimo 系统标记注入
        if self.provider_code == "mimo-free":
            payload = _inject_mimo_marker(payload)
        return payload

    async def execute_non_stream(self, request: ChatCompletionRequest) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy()) as client:
            headers = await self._build_headers(client)
            payload = self._prepare_payload(request, stream=False)
            resp = await client.post(self._url(), headers=headers, json=payload)
            # mimo 401/403 自动重试一次
            if resp.status_code in (401, 403) and self.provider_code == "mimo-free":
                _MIMO_JWT["token"] = None
                headers = await self._build_headers(client)
                resp = await client.post(self._url(), headers=headers, json=payload)
            if resp.status_code >= 400:
                _surface_free_error(self.provider_code, resp, "non_stream")
            return resp.json()

    async def execute_stream(self, request: ChatCompletionRequest) -> AsyncGenerator[dict, None]:
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy()) as client:
            headers = await self._build_headers(client)
            headers["Accept"] = "text/event-stream"
            payload = self._prepare_payload(request, stream=True)
            async with client.stream("POST", self._url(), headers=headers, json=payload) as resp:
                if resp.status_code in (401, 403) and self.provider_code == "mimo-free":
                    # mimo 401/403：清 JWT 重试一次
                    _MIMO_JWT["token"] = None
                    # 注意 acclose() 让外层退出再重试一次
                    await resp.aread()
                else:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        _resp_fake = type("R", (), {"status_code": resp.status_code, "text": body})()
                        _surface_free_error(self.provider_code, _resp_fake, "stream")
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            line = line[6:]
                        if line == "[DONE]":
                            break
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
            # 失败重试一次
            if self.provider_code == "mimo-free" and _MIMO_JWT["token"] is None:
                async with httpx.AsyncClient(timeout=self.timeout, **self._proxy()) as client2:
                    headers = await self._build_headers(client2)
                    headers["Accept"] = "text/event-stream"
                    payload = self._prepare_payload(request, stream=True)
                    async with client2.stream("POST", self._url(), headers=headers, json=payload) as resp:
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("data: "):
                                line = line[6:]
                            if line == "[DONE]":
                                break
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError:
                                continue

    async def list_models(self) -> List[str]:
        """拉取免费层模型列表。仅 opencode 提供 /models 端点；其余（mimo-free 等）返回空。"""
        if self.provider_code != "opencode":
            return []
        url = self.meta["base_url"].rstrip("/") + "/zen/v1/models"
        headers = {
            "Authorization": "Bearer public",
            "x-opencode-client": "desktop",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout, **self._proxy()) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"opencode models HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                data = resp.json()
            except Exception:
                return []
            return [m.get("id") for m in data.get("data", []) if m.get("id")]


# ── 单例缓存 ──
_EXECUTORS: Dict[str, FreeProviderExecutor] = {}


def get_free_executor(code: str) -> Optional[FreeProviderExecutor]:
    if code not in _FREE_PROVIDERS_META:
        return None
    if code not in _EXECUTORS:
        _EXECUTORS[code] = FreeProviderExecutor(code)
    return _EXECUTORS[code]
