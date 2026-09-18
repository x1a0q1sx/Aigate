"""QoderAdapter — 把 OpenAI 格式请求代理到 Qoder 的 COSY 签名推理端点。

端口自 9router open-sse/executors/qoder.js（704 行）+ qoderModels.js +
shared/qoder/{sse,contextTier}.js。与原版差异（有意为之）：

- 图片不做 OSS 上传（qodercli 的 /api/v2/image/upload），data URI 直接内联；
  超过 512KB 的二进制替换为短 stub（对齐原版的兜底语义）。
- 其余保持等价：WAF 编码（&Encode=1 + latin1 body）、COSY 17 头、
  服务端下发的 per-model model_config（缺失硬错误——发错 config 会被
  静默降级到别的模型）、{statusCodeValue, body} SSE 信封解包 +
  finish/usage 合流、首帧计费拦截（112/10605/pricingUrl）直接抛错走回退。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from typing import AsyncGenerator, Dict, List, Optional

import httpx

from server.adapters.base_adapter import (BaseAdapter, HealthResult, ModelInfo)
from server.core.qoder_sign import (
    QODER_JOB_TOKEN_EXCHANGE_URL, QODER_USERINFO_URL,
    build_cosy_headers, chat_url, model_list_url, qoder_encode_body,
)

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 3600
PAT_TTL_MS_DEFAULT = 24 * 3600 * 1000
MAX_INLINE_IMAGE_BYTES = 512 * 1024
# 上下文档位自动升档（9router QODER_CONTEXT_TIER_HEADROOM）
TIER_HEADROOM = 0.15
_CJK_RE = re.compile(r"[ᄀ-ᇿ⺀-鿿가-힯豈-﫿＀-￯]")

_CATALOG: Dict[str, tuple] = {}      # key -> (expires_monotonic, catalog dict)
_INFLIGHT: Dict[str, asyncio.Task] = {}
_PAT_JOBS: Dict[str, dict] = {}       # pt-xxx -> {token, uid, exp}

_QODER_STATIC_HINT = {
    "auto", "ultimate", "performance", "efficient", "lite",
    "qmodel", "qfmodel", "qmodel_latest", "qmodel_38max",
    "kmodel", "kmodel_latest", "gmodel", "gfmodel",
    "dmodel", "dfmodel", "mmodel",
}


def _stable_hash(prefix: str, *parts) -> str:
    h = hashlib.sha256()
    h.update(prefix.encode())
    for p in parts:
        h.update(b"\x00")
        h.update(str(p).encode("utf-8", "replace"))
    return h.hexdigest()[:32]


def _is_billing_block(inner) -> bool:
    if not isinstance(inner, str):
        return False
    return bool(re.search(r'"code"\s*:\s*"(112|10605)"', inner)) or \
        "pricingurl" in inner.lower()


# ── 消息规范化 ───────────────────────────────────────

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _normalize_content(content):
    """文本压平为 str；有图保持数组（OpenAI image_url 形态）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    blocks, text_parts, has_image = [], [], False

    def push_text(t):
        if not t:
            return
        if has_image or blocks:
            blocks.append({"type": "text", "text": t})
        else:
            text_parts.append(t)

    for item in content:
        if not isinstance(item, dict):
            continue
        it = item.get("type")
        if it == "text":
            push_text(str(item.get("text") or ""))
        elif it == "image_url":
            iu = item.get("image_url")
            url = iu if isinstance(iu, str) else (iu or {}).get("url")
            if url:
                if len(url) > MAX_INLINE_IMAGE_BYTES:
                    push_text(f"[image attached: {url[:64]}...]")
                else:
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
                    has_image = True
        elif it == "image" and item.get("source"):
            src = item["source"]
            if src.get("type") == "base64" and src.get("data"):
                url = f"data:{src.get('media_type') or 'image/png'};base64,{src['data']}"
            else:
                url = str(src.get("url") or "")
            if url:
                if len(url) > MAX_INLINE_IMAGE_BYTES:
                    push_text(f"[image attached: {url[:64]}...]")
                else:
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
                    has_image = True
    if blocks or has_image:
        if text_parts:
            blocks.insert(0, {"type": "text", "text": "\n".join(text_parts)})
        return blocks
    return "\n".join(text_parts)


def _normalize_messages(messages: List[dict]):
    """role:system 提出为顶层 system 文本（Qoder 拒绝 messages 里的 system）。"""
    system_parts, out = [], []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            t = _extract_text(msg.get("content"))
            if t:
                system_parts.append(t)
            continue
        cloned = dict(msg)
        cloned["content"] = _normalize_content(msg.get("content"))
        out.append(cloned)
    return out, "\n\n".join(system_parts)


# ── 上下文档位（auto 升档） ──────────────────────────

def _parse_tier_count(value) -> int:
    if isinstance(value, (int, float)):
        return int(value) if value and value > 0 else 0
    s = str(value or "").strip().upper()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KM])?$", s)
    if not m:
        return 0
    n = float(m.group(1)) * {"K": 1000, "M": 1_000_000}.get(m.group(2) or "", 1)
    return int(n) if n > 0 else 0


def _context_tiers(model_config: dict) -> List[dict]:
    raw = (model_config or {}).get("context_config") or (model_config or {}).get("contextConfig")
    if not isinstance(raw, list):
        return []
    by_count: Dict[int, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        count = _parse_tier_count(
            entry.get("tokenCount") or entry.get("token_count")
            or entry.get("max_input_tokens") or entry.get("maxInputTokens")
            or entry.get("context_length") or entry.get("contextLength"))
        if not count:
            continue
        is_default = bool(entry.get("isDefault") or entry.get("is_default") or entry.get("default"))
        prev = by_count.get(count, {})
        name = str(entry.get("name") or entry.get("label") or entry.get("display_name") or "").strip()
        if not name:
            if count >= 1_000_000 and count % 1_000_000 == 0:
                name = f"{count // 1_000_000}M"
            elif count >= 1000 and count % 1000 == 0:
                name = f"{count // 1000}K"
            else:
                name = str(count)
        by_count[count] = {"name": name, "token_count": count,
                           "is_default": (prev.get("is_default") or is_default)}
    return sorted(by_count.values(), key=lambda t: t["token_count"])


def _estimate_prompt_tokens(system: str, messages: list, tools: list) -> int:
    try:
        text = json.dumps({"system": system or "", "messages": messages or [],
                           "tools": tools or []}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return 0
    cjk = len(_CJK_RE.findall(text))
    return int(cjk + (len(text) - cjk) / 4) + 1


# ── 适配器 ──────────────────────────────────────────

class QoderAdapter(BaseAdapter):
    """Qoder dt-/jt-/pt- 凭证 → COSY 签名 agent_chat_generation 代理。"""

    def __init__(self, timeout: Optional[int] = None):
        self._connect_timeout = 30.0
        self._read_timeout = float(timeout) if timeout else 300.0

    # ── 凭证解析 ─────────────────────────────────────
    async def _resolve_credentials(self, api_key: str) -> tuple:
        """→ (token, creds_dict)。PAT(pt-) 先换 job token；dt- 从连接 meta 补 uid。"""
        token = (api_key or "").strip()
        if token.startswith("pt-"):
            job = _PAT_JOBS.get(token)
            if not job or job["exp"] - time.time() * 1000 < 5 * 60 * 1000:
                job = await self._exchange_pat(token)
                _PAT_JOBS[token] = job
            return job["token"], {
                "user_id": job["uid"], "auth_token": job["token"],
                "name": "", "email": "", "machine_id": "",
            }
        meta = await self._token_meta()
        uid = str(meta.get("uid") or "")
        if not uid:
            raise RuntimeError("qoder 凭证缺少 user_id（请重新连接 Qoder 账号）")
        return token, {
            "user_id": uid, "auth_token": token,
            "name": str(meta.get("name") or ""), "email": str(meta.get("email") or ""),
            "machine_id": str(meta.get("machine_id") or ""),
        }

    @staticmethod
    async def _exchange_pat(pat: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    QODER_JOB_TOKEN_EXCHANGE_URL,
                    json={"personal_token": pat},
                    headers={"Content-Type": "application/json",
                             "Accept": "application/json",
                             "User-Agent": "qodercli/1.0.0",
                             "Cosy-Version": "1.0.0", "Cosy-ClientType": "5"})
            if r.status_code >= 400:
                raise RuntimeError(f"qoder PAT exchange failed: {r.status_code} {r.text[:120]}")
            data = r.json()
            jt = str(data.get("token") or "")
            if not jt:
                raise RuntimeError("qoder PAT exchange returned no job token")
            exp = time.time() * 1000 + PAT_TTL_MS_DEFAULT
            if data.get("expires_at"):
                from datetime import datetime as _dt
                try:
                    exp = _dt.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00")).timestamp() * 1000
                except ValueError:
                    pass
            uid = ""
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    ur = await client.get(QODER_USERINFO_URL, headers={
                        "Authorization": f"Bearer {jt}", "Accept": "application/json",
                        "User-Agent": "Go-http-client/2.0"})
                if ur.is_success:
                    ud = ur.json() or {}
                    uid = str(ud.get("id") or ud.get("userId") or ud.get("user_id") or "")
            except httpx.HTTPError:
                pass
            return {"token": jt, "uid": uid, "exp": exp}
        except httpx.HTTPError as e:
            raise RuntimeError(f"qoder PAT exchange network error: {e}")

    @staticmethod
    async def _token_meta() -> dict:
        """读 qoder 连接记录里 scope 列存的签名元数据（uid/machine_id/email/name）。"""
        from server.db import AsyncSessionLocal
        from server.core.oauth_client import get_oauth_client
        async with AsyncSessionLocal() as db:
            return await get_oauth_client().get_token_meta("qoder", db)

    # ── 模型目录 ─────────────────────────────────────
    def _cache_key(self, creds: dict) -> str:
        seed = creds.get("user_id") or creds.get("auth_token") or "anon"
        return hashlib.sha256(f"qoder:{seed}".encode()).hexdigest()[:24]

    async def _catalog(self, creds: dict, force: bool = False) -> Optional[dict]:
        key = self._cache_key(creds)
        now = time.monotonic()
        hit = _CATALOG.get(key)
        if hit and hit[0] > now and not force:
            return hit[1]
        if not (creds.get("user_id") and creds.get("auth_token")):
            return None
        inflight = _INFLIGHT.get(key)
        if inflight and not force:
            return await asyncio.shield(inflight)

        async def _fetch():
            url = model_list_url(creds["auth_token"])
            headers = {"Accept": "application/json", "Accept-Encoding": "identity",
                       **build_cosy_headers(b"", url, creds)}
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.get(url, headers=headers)
                if not r.is_success:
                    logger.warning("qoder catalog http %s", r.status_code)
                    return None
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("qoder catalog fetch failed: %s", e)
                return None
            chat = (body or {}).get("chat")
            if not isinstance(chat, list):
                return None
            raw_configs, models = {}, []
            for entry in chat:
                if not isinstance(entry, dict) or not entry.get("key"):
                    continue
                raw_configs[entry["key"]] = entry
                if entry.get("enable") is False:
                    continue
                models.append(entry)
            result = {"raw_configs": raw_configs, "models": models}
            _CATALOG[key] = (time.monotonic() + CATALOG_TTL_SECONDS, result)
            return result

        task = asyncio.get_event_loop().create_task(_fetch())
        _INFLIGHT[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if _INFLIGHT.get(key) is task:
                _INFLIGHT.pop(key, None)

    async def _model_config(self, creds: dict, qoder_key: str) -> dict:
        for force in (False, True):
            cat = await self._catalog(creds, force=force)
            cfg = (cat or {}).get("raw_configs", {}).get(qoder_key)
            if cfg:
                return {**cfg, "key": qoder_key}
        raise RuntimeError(
            f"qoder: 模型 '{qoder_key}' 的 model_config 未知（上游目录未收录；"
            f"已知键示例：{sorted((await self._catalog(creds) or {}).get('raw_configs', {}).keys())[:6]}…）")

    # ── 请求构造 ─────────────────────────────────────
    def _build_payload(self, body: dict, qoder_key: str, model_config: dict,
                       creds: dict) -> dict:
        messages, system_text = _normalize_messages(body.get("messages") or [])
        tools = body.get("tools") if isinstance(body.get("tools"), list) else []
        max_out = int(model_config.get("max_output_tokens") or 0)
        max_tokens = max_out if max_out > 0 else 32768
        for field in ("max_tokens", "max_completion_tokens"):
            v = body.get(field)
            if isinstance(v, (int, float)) and 0 < v < max_tokens:
                max_tokens = int(v)
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = _extract_text(m.get("content"))
                break
        session_id = _stable_hash("qoder-session", creds.get("user_id"), qoder_key)
        record_id = _stable_hash(
            "qoder-record", qoder_key, len(messages), last_user[:512],
            [t.get("function", {}).get("name") if isinstance(t, dict) else "" for t in tools],
            max_tokens)

        payload = {
            "request_id": str(uuid.uuid4()),
            "request_set_id": record_id,
            "chat_record_id": record_id,
            "session_id": session_id,
            "stream": True,
            "chat_task": "FREE_INPUT",
            "is_reply": True,
            "is_retry": False,
            "source": 1,
            "version": "3",
            "session_type": "qodercli",
            "agent_id": "agent_common",
            "task_id": "common",
            "code_language": "",
            "chat_prompt": "",
            "image_urls": None,
            "aliyun_user_type": "",
            "system": system_text,
            "messages": messages,
            "tools": tools,
            "parameters": {"max_tokens": max_tokens},
            "chat_context": {
                "chatPrompt": "",
                "imageUrls": None,
                "extra": {
                    "context": [],
                    "modelConfig": {"key": qoder_key,
                                    "is_reasoning": bool(model_config.get("is_reasoning"))},
                    "originalContent": last_user,
                },
                "features": [],
                "text": last_user,
            },
            "model_config": model_config,
            "business": {
                "product": "cli", "version": "1.0.0", "type": "agent",
                "stage": "start", "id": str(uuid.uuid4()),
                "name": last_user[:30], "begin_at": int(time.time() * 1000),
            },
        }

        # 上下文档位：默认档装不下时升档（9router auto 模式语义）
        tiers = _context_tiers(model_config)
        if tiers:
            default_tier = next((t for t in tiers if t["is_default"]), tiers[0])
            current = _parse_tier_count(model_config.get("max_input_tokens")) or default_tier["token_count"]
            est = _estimate_prompt_tokens(system_text, messages, tools)
            need = int(est * (1 + TIER_HEADROOM) + 0.999)
            if need > current:
                pick = next((t for t in tiers if t["token_count"] >= need), tiers[-1])
                if pick["token_count"] > current:
                    payload["parameters"]["context_length"] = pick["token_count"]
                    payload["chat_context"]["extra"]["ideModelConfigOverride"] = {
                        "max_input_tokens": pick["token_count"]}
                    payload["model_config"] = {**model_config,
                                               "max_input_tokens": pick["token_count"]}
        return payload

    def _request_headers(self, token: str, qoder_key: str, model_config: dict) -> dict:
        # 在 _stream_call 内基于编码后 body 补齐 COSY 头
        source = (model_config or {}).get("source") or "system"
        return {"X-Model-Key": qoder_key, "X-Model-Source": source}

    # ── SSE 信封解包 + finish/usage 合流（9router createQoderSseCoalescer） ──
    class _Coalescer:
        def __init__(self, model: str):
            self.model = model
            self.pending_finish = None
            self.pending_usage = None
            self.last_meta = {"id": None, "created": None, "model": model}
            self.terminal = False
            self.finish_forwarded = False

        @staticmethod
        def _usage(parsed):
            usage = parsed.get("usage") if isinstance(parsed, dict) else None
            if not isinstance(usage, dict):
                return None
            p = usage.get("prompt_tokens")
            c = usage.get("completion_tokens")
            if p is None and c is None:
                return None
            out = {"prompt_tokens": int(p or 0), "completion_tokens": int(c or 0),
                   "total_tokens": int(usage.get("total_tokens") or (p or 0) + (c or 0))}
            for extra in ("prompt_tokens_details", "completion_tokens_details"):
                if isinstance(usage.get(extra), dict):
                    out[extra] = usage[extra]
            return out

        def _terminal_chunk(self):
            ch = {
                "id": self.last_meta["id"] or f"qoder-{int(time.time() * 1000)}",
                "object": "chat.completion.chunk",
                "created": self.last_meta["created"] or int(time.time()),
                "model": self.last_meta["model"] or self.model,
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": self.pending_finish or "stop"}],
            }
            if self.pending_usage:
                ch["usage"] = self.pending_usage
            self.pending_finish = None
            self.pending_usage = None
            return ch

        def handle(self, inner) -> List[dict]:
            if self.terminal:
                return []
            if inner == "[DONE]":
                self.terminal = True
                return self.flush()
            if not isinstance(inner, (str, dict)):
                return []
            try:
                parsed = json.loads(inner) if isinstance(inner, str) else inner
            except ValueError:
                return [{"id": f"qoder-raw-{int(time.time() * 1000)}",
                         "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": self.model,
                         "choices": [{"index": 0, "delta": {"content": str(inner)},
                                      "finish_reason": None}]}]
            if not isinstance(parsed, dict):
                return []
            if parsed.get("id"):
                self.last_meta["id"] = parsed["id"]
            if isinstance(parsed.get("created"), int):
                self.last_meta["created"] = parsed["created"]
            if parsed.get("model"):
                self.last_meta["model"] = parsed["model"]
            usage = self._usage(parsed)
            if usage:
                self.pending_usage = usage
            choice = (parsed.get("choices") or [{}])[0] if parsed.get("choices") else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else None
            finish = (choice.get("finish_reason")
                      or (delta or {}).get("finish_reason")
                      or parsed.get("finish_reason"))
            valuable = bool(delta) and (
                delta.get("content") or delta.get("reasoning_content")
                or delta.get("tool_calls") or delta.get("role"))
            out = []
            if valuable:
                out.append(parsed)
                if finish:
                    self.finish_forwarded = True
                    self.pending_finish = finish if self.pending_usage else None
                if self.pending_finish and self.pending_usage:
                    out.append(self._terminal_chunk())
                    self.terminal = True
                return out
            if finish:
                self.pending_finish = finish
            if self.pending_finish and self.pending_usage:
                out.append(self._terminal_chunk())
                self.terminal = True
            return out

        def flush(self) -> List[dict]:
            if self.terminal:
                return []
            out = []
            if self.pending_usage or (self.pending_finish and not self.finish_forwarded):
                out.append(self._terminal_chunk())
            self.terminal = True
            return out

    # ── 核心流式调用 ─────────────────────────────────
    async def stream_chat_completion(self, request, api_key, base_url,
                                     extra_headers=None) -> AsyncGenerator[dict, None]:
        body = request.model_dump() if hasattr(request, "model_dump") else dict(request)
        qoder_key = str(body.get("model") or "auto")
        if qoder_key.startswith("qoder/"):
            qoder_key = qoder_key[len("qoder/"):]
        token, creds = await self._resolve_credentials(api_key)
        model_config = await self._model_config(creds, qoder_key)
        payload = self._build_payload(body, qoder_key, model_config, creds)

        plain = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        encoded = qoder_encode_body(plain)
        url = chat_url(creds["auth_token"])
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "Accept-Encoding": "identity",  # gzip 会触发 CDN 签名校验
            **self._request_headers(token, qoder_key, model_config),
            **build_cosy_headers(encoded, url, creds),
        }
        co = self._Coalescer(f"qoder/{qoder_key}")
        first_frame = True
        timeout = httpx.Timeout(self._connect_timeout, read=self._read_timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, content=encoded) as resp:
                if resp.status_code != 200:
                    detail = ""
                    try:
                        detail = (await resp.aread()).decode("utf-8", "replace")[:200]
                    except Exception:
                        pass
                    raise RuntimeError(f"qoder http {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        for c in co.flush():
                            yield c
                        return
                    try:
                        envelope = json.loads(data)
                    except ValueError:
                        continue
                    status_val = envelope.get("statusCodeValue")
                    status_val = status_val if isinstance(status_val, int) else 200
                    inner = envelope.get("body")
                    if not isinstance(inner, str):
                        inner = "" if inner is None else json.dumps(inner, ensure_ascii=False)
                    if status_val != 200:
                        if first_frame and _is_billing_block(inner):
                            raise RuntimeError(f"qoder 计费/额度拦截：{inner[:200]}")
                        for c in co.flush():
                            yield c
                        yield {"error": f"qoder upstream status {status_val}: {inner[:200]}"}
                        return
                    first_frame = False
                    for c in co.handle(inner):
                        yield c
                    if co.terminal:
                        return
                for c in co.flush():
                    yield c

    # ── 非流式：聚合自身流（Qoder 推理端点只有 SSE） ─────
    async def chat_completion(self, request, api_key, base_url, extra_headers=None) -> dict:
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: Dict[int, dict] = {}
        finish = None
        usage = {}
        model_name = str(getattr(request, "model", "") or "auto")
        async for chunk in self.stream_chat_completion(request, api_key, base_url, extra_headers):
            if isinstance(chunk, dict) and chunk.get("error"):
                raise RuntimeError(str(chunk["error"]))
            if isinstance(chunk, dict) and chunk.get("usage"):
                usage = chunk["usage"]
            for choice in (chunk.get("choices") or []):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index") or 0)
                    slot = tool_calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        message = {"role": "assistant", "content": "".join(content_parts) or None}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
            if not message["content"]:
                message["content"] = None
        return {
            "id": f"chatcmpl-qoder-{uuid.uuid4().hex[:20]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish or "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # ── 模型目录 ─────────────────────────────────────
    async def list_models(self, api_key, base_url, extra_headers=None) -> List[ModelInfo]:
        token, creds = await self._resolve_credentials(api_key)
        cat = await self._catalog(creds, force=False)
        if not cat:
            raise RuntimeError("qoder 模型目录拉取失败（检查连接/网络）")
        out = []
        for entry in cat["models"]:
            out.append(ModelInfo(
                model_id=entry["key"],
                display_name=entry.get("display_name") or entry["key"],
                is_free=False, input_price=0.0, output_price=0.0,
                supports_streaming=True,
                supports_vision=bool(entry.get("is_vl")),
                context_length=int(entry.get("max_input_tokens") or 131072),
            ))
        return out

    # ── 健康探测（拉目录，轻量且需签名） ─────────────────
    async def health_check(self, model, api_key, base_url,
                           extra_headers=None, timeout: int = 10) -> HealthResult:
        start = time.time()
        try:
            token, creds = await self._resolve_credentials(api_key)
            cat = await self._catalog(creds)
            if cat is None:
                return HealthResult(status="unhealthy",
                                    latency_ms=(time.time() - start) * 1000,
                                    error_message="qoder catalog fetch failed")
            keys = set(cat.get("raw_configs") or {})
            ok = (not model) or (model in keys) or (f"qoder/{model}" in keys)
            return HealthResult(
                status="healthy" if ok else "degraded",
                latency_ms=(time.time() - start) * 1000,
                error_message="" if ok else f"model {model} not in catalog")
        except Exception as e:
            return HealthResult(status="unhealthy",
                                latency_ms=(time.time() - start) * 1000,
                                error_message=str(e)[:180])
