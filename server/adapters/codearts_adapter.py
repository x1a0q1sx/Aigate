"""CodeArtsAdapter — 把 OpenAI 格式请求代理到华为 CodeArts 的签名推理端点。

与 openai_compat 的差别（每一条都是 Jet-Hub e2e 实测结论，见 codearts 模块 docstring）：

- **不是 Bearer，是 SDK-HMAC-SHA256 请求签名**：api_key 传的是一段 JSON
  凭据（AK/SK/SecurityToken + refresh_token + DPoP 私钥），逐请求现签。
- `glm-5.3-flash` 必须带 `maas_type: benefit` 头**且该头参与签名**。
- deepseek-v4 系切换 **DSML 工具调用模式**（tools schema 注入 system、
  **不发 `tools` 字段**），否则大参数写入必然被 APIG 网关的 ~60s 空闲断连掐断。
- assistant 历史**必须带 `reasoning_content` 字段**（缺失直接 400）。
- 并发超限（`TM.00001041`）需轮询 `/api/v1/queue/status`，最多 30 分钟。
- SSE 只有流式：`_stream_chat_completion` 是唯一出口，非流式由自身聚合。

api_key 形态（由集成层构造，见汇报里的集成说明）：
  JSON 字符串 {"access_key_id", "secret_access_key", "security_token",
              "expires_at", "model"?, "owner"?}
旧式裸 token（非 JSON）会被拒绝并给出明确说明 —— 静默当成 AK 用只会得到
一个看不出根因的验签 401。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from server.adapters.base_adapter import BaseAdapter, HealthResult, ModelInfo
from server.core import codearts as ca

logger = logging.getLogger(__name__)

# 对外错误文案的 provider 前缀（日志/异常里统一可读）
_PROVIDER = "codearts"


class CodeArtsAdapter(BaseAdapter):
    """CodeArts 签名推理端点适配器（OpenAI 兼容的出站形态）。"""

    def __init__(self, timeout: Optional[int] = None):
        self._connect_timeout = 30.0
        self._read_timeout = float(timeout) if timeout else ca.SSE_CHUNK_TIMEOUT_SECONDS

    # ── 凭据 ─────────────────────────────────────────
    def _resolve_credentials(self, api_key: str) -> dict:
        """api_key(JSON) → 凭据 dict。**永不抛异常**，失败时 `_error` 带说明。

        为什么不用异常：适配器的四个接口各有自己的错误表达方式
        （stream 发 error chunk、health 发 unhealthy、list_models 回退静态种子），
        统一成异常会让每个调用点都要写一遍 try。
        """
        raw = api_key if isinstance(api_key, str) else ""
        raw = raw.strip()
        if not raw:
            return {"_error": "CodeArts 凭据为空"}
        if not raw.startswith("{"):
            return {"_error": ("CodeArts 需要 JSON 凭据（含 access_key_id / "
                              "secret_access_key / security_token），"
                              "不支持裸 token；请重新连接该账号")}
        try:
            data = json.loads(raw)
        except ValueError:
            return {"_error": "CodeArts 凭据不是合法 JSON；请重新连接该账号"}
        if not isinstance(data, dict):
            return {"_error": "CodeArts 凭据应为 JSON 对象"}
        cred = {
            "access_key_id": str(data.get("access_key_id") or ""),
            "secret_access_key": str(data.get("secret_access_key") or ""),
            "security_token": str(data.get("security_token") or ""),
            "expires_at": str(data.get("expires_at") or ""),
        }
        if not (cred["access_key_id"] and cred["secret_access_key"]
                and cred["security_token"]):
            return {"_error": ("CodeArts 凭据缺少 AK/SK/SecurityToken —— "
                              "旧 ticket 流程的凭据不含 SK，需要重新走 OAuth 登录"),
                    **cred}
        return cred

    @staticmethod
    def _tools_of(request) -> List[dict]:
        tools = getattr(request, "tools", None)
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
    # ── 请求体 ───────────────────────────────────────
    def _build_body(self, request, session_id: str,
                    tools: List[dict]) -> Tuple[dict, bool]:
        """→ (body, dsml)。dsml=True 时**不发** tools 字段（改走 system 注入）。"""
        model = str(getattr(request, "model", "") or "")
        dsml = ca.needs_dsml_tool_mode(model) and bool(tools)
        body = ca.build_chat_body(request, session_id=session_id,
                                  dsml=dsml, tools=tools)
        return body, dsml

    # ── 核心流式调用 ─────────────────────────────────
    async def stream_chat_completion(self, request, api_key, base_url,
                                     extra_headers=None) -> AsyncGenerator[dict, None]:
        """流式代理。yield OpenAI 格式 chunk（含 reasoning_content / tool_calls）。

        并发超限与鉴权失败的重试都在本层完成：
        - `TM.00001041`（或 SSE 内嵌排队错误）→ 轮询排队状态后重试，上限 30 分钟；
        - `APIG.0602`/401/403 → 凭据刷新回调（`extra_headers["__refresh"]`）后重试一次。

        排队期间**不产出任何 chunk**：StreamChunk 协议没有独立的瞬态状态通道，
        任何 reasoning/text 块都会被上层组装进 assistant 消息并持久化
        （`src/llm-adapter.ts:1035-1044` 的实测教训）。
        """
        cred = self._resolve_credentials(api_key)
        if cred.get("_error") and not cred.get("access_key_id"):
            yield {"error": f"{_PROVIDER}: {cred['_error']}"}
            return

        model = str(getattr(request, "model", "") or "")
        session_id = self._session_id(request, extra_headers)
        chat_id = ca.new_chat_id()
        tools = self._tools_of(request)
        body, dsml = self._build_body(request, session_id, tools)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = ca.chat_url()

        queue_attempts = 0
        auth_refreshed = False
        while True:
            headers = ca.build_chat_headers(cred, model, payload,
                                           chat_id=chat_id, session_id=session_id)
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "text/event-stream"
            headers["Cache-Control"] = "no-cache"
            headers["Accept-Encoding"] = "identity"
            status_code = 0
            error_text = ""
            try:
                async with httpx.AsyncClient(
                        timeout=httpx.Timeout(self._connect_timeout,
                                              read=self._read_timeout)) as client:
                    async with client.stream("POST", url, headers=headers,
                                             content=payload) as resp:
                        if resp.status_code < 400:
                            async for chunk in self._consume_sse(resp, model, dsml):
                                if isinstance(chunk, dict) and chunk.get("__retry_queue"):
                                    break
                                if isinstance(chunk, dict) and chunk.get("error"):
                                    # 流内致命错误（非排队）：直接透传，不进排队重试
                                    yield chunk
                                    return
                                yield chunk
                            else:
                                # 流正常结束（含 [DONE] 与 finish 帧）
                                return
                            # 走到这里 = 内嵌排队错误：落到下方排队重试
                        else:
                            status_code = resp.status_code
                            try:
                                error_text = (await resp.aread()).decode(
                                    "utf-8", "replace")[:400]
                            except Exception:
                                error_text = ""
            except Exception as e:
                if ca.is_transport_error(e):
                    # APIG 网关 ~60s 空闲断连：归类为可重试的瞬态故障，
                    # 让上层有机会重试（当成正常结束会静默丢内容）
                    yield {"error": f"{_PROVIDER}: sse transport error: {e}"}
                    return
                yield {"error": f"{_PROVIDER}: {type(e).__name__}: {e}"}
                return

            if status_code:
                # 鉴权失败：静默刷新一次再试（入口的 expires_at 预判覆盖不到
                # 「后端提前吊销 / 本地时钟偏差」这两种情况）
                if ca.is_auth_error(status_code, error_text) and not auth_refreshed:
                    refreshed = await self._try_refresh(extra_headers)
                    if refreshed is not None:
                        auth_refreshed = True
                        cred = refreshed
                        continue
                    yield {"error": f"{_PROVIDER}: 鉴权失败且无法续期"
                                    f"（HTTP {status_code}）：{error_text[:200]}"}
                    return
                # 非命中并发超限文案的错误未必不在排队：先探状态端点，
                # 只有它确认 waiting/queue_full 才排队；否则按原错误分类立即
                # 抛出，避免把真错误（401/400）拖成 30 分钟超时
                if not ca.is_queue_error(status_code, error_text):
                    probe = await self._query_queue_status(cred, model, session_id)
                    if probe is None or probe.get("status") == "working":
                        code = ca.http_error_code(status_code, error_text)
                        yield {"error": f"{_PROVIDER}: model request failed with "
                                        f"HTTP {status_code} ({code}): {error_text[:200]}"}
                        return

            # 排队流程：每次重试前查状态，终态立即抛错；否则等 10s 直接重试
            queue_attempts += 1
            if queue_attempts > ca.QUEUE_MAX_ATTEMPTS:
                yield {"error": f"{_PROVIDER}: queue wait timed out after 30 minutes"}
                return
            status = await self._query_queue_status(cred, model, session_id)
            if status and status.get("status") in ("error", "queue_full"):
                yield {"error": f"{_PROVIDER}: {status.get('message') or status['status']}"}
                return
            await asyncio.sleep(ca.QUEUE_RETRY_DELAY_SECONDS)

    @staticmethod
    def _tools_of(request) -> List[dict]:
        tools = getattr(request, "tools", None)
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    @staticmethod
    def _session_id(request, extra_headers) -> str:
        """会话 id = prompt_cache_key：缺失时缓存命中恒为 0（实测 2026-08-24）。

        用调用方给的 session/会话标识（extra_headers 透传）；都没有时按
        模型+首条 user 文本派生一个稳定值 —— 同一会话的多次请求会命中同一缓存，
        随机 uuid 会让缓存永远打不中。
        """
        eh = extra_headers or {}
        for key in ("__codearts_session", "session_id", "conversation_id"):
            v = str(eh.get(key) or "").strip()
            if v:
                return v
        import hashlib
        seed = (str(getattr(request, "model", "") or "")
                + json.dumps([getattr(m, "content", "") for m in
                              (getattr(request, "messages", None) or [])[:1]],
                             ensure_ascii=False, default=str))
        return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:32]

    async def _try_refresh(self, extra_headers) -> Optional[dict]:
        """调用集成层注入的续期回调（`extra_headers["__codearts_refresh"]`）。

        ⚠️ 回调内部必须**串行化**：CodeArts 的 refresh_token 一次性轮换，
        并发刷新会互相作废（见 core/codearts.py`refresh_token` 的 docstring）。
        AIGate 的 oauth_client 已有 per (provider, owner) 的单飞锁，集成层
        应复用它。回调返回新的凭据 JSON 字符串（或 None 表示无法续期）。
        """
        fn = (extra_headers or {}).get("__codearts_refresh")
        if fn is None:
            return None
        try:
            new_raw = fn() if not asyncio.iscoroutinefunction(fn) else await fn()
            if hasattr(new_raw, "__await__"):
                new_raw = await new_raw
        except Exception as e:
            logger.warning("codearts refresh callback failed: %s", e)
            return None
        if not new_raw:
            return None
        cred = self._resolve_credentials(
            new_raw if isinstance(new_raw, str) else json.dumps(new_raw))
        return cred if not cred.get("_error") else None

    async def _query_queue_status(self, cred: dict, model: str,
                                  task_id: str) -> Optional[dict]:
        """`GET /api/v1/queue/status`（签名 GET，无 body 故不加 content-type）。"""
        url = ca.queue_status_url(model, task_id)
        headers = ca.signed_headers_for_request(
            cred.get("access_key_id", ""), cred.get("secret_access_key", ""),
            cred.get("security_token", ""), "GET", url, b"")
        headers["x-snap-traceid"] = ca.new_chat_id()
        headers["Agent-Type"] = "INFERHUB_AGENT"
        headers["X-Language"] = "en"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return None
            return ca.parse_queue_status(r.json())
        except Exception:
            # 端点不可达/返回形态不可识别 → None（调用方按原错误分类抛出，
            # 避免把真错误拖成 30 分钟超时）
            return None

    async def _consume_sse(self, resp, model: str, dsml: bool) -> AsyncGenerator[dict, None]:
        """消费 SSE → OpenAI chunk。

        - `delta.content` 走 DSML 提取器：`<thought>` 内容分流到 reasoning、
          DSML 块解析为 tool_calls、其余作为正文；
        - `delta.reasoning_content` 走**独立**提取器（共用会互相污染状态机），
          其 `text + reasoning` 全部按 reasoning 输出 —— 否则 deepseek-v4 的
          思考会泄漏到正文（`src/llm-adapter.ts:1247-1266`）；
        - `data.error_code` 命中排队/限流错误码时 yield `{"__retry_queue": True}`
          让外层走排队重试（HTTP 200 也可能带限流错误）。

        为什么不做 Jet-Hub 的「正文为空时用推理填充可见区」回退：那是 agent 循环
        在**流结束后**才知道的事，而本适配器是把 chunk 直接透给客户端（与
        openai_compat 同构），流到中途无从判断。GLM 偶尔整段回答走
        reasoning_content 的情况原样透出（客户端仍可在思考区看到），
        与仓库其它适配器口径一致。
        """
        content_ex = ca.DsmlContentExtractor()
        reasoning_ex = ca.DsmlContentExtractor()
        finish_reason = None
        usage = None
        chunk_meta = {"id": f"chatcmpl-codearts-{ca.new_chat_id()[:20]}",
                      "created": int(time.time()), "model": model}
        tool_index = 0

        def _chunk(delta: dict, finish=None) -> dict:
            return {
                "id": chunk_meta["id"], "object": "chat.completion.chunk",
                "created": chunk_meta["created"], "model": chunk_meta["model"],
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}],
            }

        def _emit_text(text: str) -> dict:
            return _chunk({"content": text})

        def _emit_reasoning(text: str) -> dict:
            return _chunk({"reasoning_content": text})

        def _emit_tool(call: dict, cid: str) -> dict:
            nonlocal tool_index
            out = _chunk({"tool_calls": [{
                "index": tool_index, "id": cid, "type": "function",
                "function": {"name": call.get("name") or "",
                             "arguments": call.get("arguments") or "{}"},
            }]})
            tool_index += 1
            return out

        async for line in resp.aiter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            # SSE 内嵌错误：HTTP 200 + error_code（如 InferHub.ModelArts.81111.429
            # TPM 超限）。不识别它会把流当正常结束 → 表现为「思考后无输出」。
            code = data.get("error_code")
            if isinstance(code, str) and code:
                if ca.is_sse_queue_error_code(code):
                    yield {"__retry_queue": True}
                    return
                yield {"error": f"{_PROVIDER}: "
                                f"{data.get('error_msg') or code}"}
                return
            choices = data.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else {}
            if not isinstance(choice, dict):
                choice = {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if isinstance(choice.get("finish_reason"), str):
                finish_reason = choice["finish_reason"]
            content = delta.get("content")
            if isinstance(content, str) and content:
                out = content_ex.feed(content)
                if out["text"]:
                    yield _emit_text(str(out["text"]))
                if out["reasoning"]:
                    yield _emit_reasoning(str(out["reasoning"]))
                for call in out["tool_calls"] or []:
                    yield _emit_tool(call, f"dsml-{ca.new_chat_id()}")
            rc = delta.get("reasoning_content")
            if isinstance(rc, str) and rc:
                out = reasoning_ex.feed(rc)
                # text + reasoning 合并后全按 reasoning 输出（见 docstring）
                thinking = str(out["text"] or "") + str(out["reasoning"] or "")
                if thinking:
                    yield _emit_reasoning(thinking)
                for call in out["tool_calls"] or []:
                    yield _emit_tool(call, f"dsml-{ca.new_chat_id()}")
            for call in (delta.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                yield _chunk({"tool_calls": [{
                    "index": int(call.get("index") or 0),
                    "id": str(call.get("id") or ""),
                    "type": "function",
                    "function": {"name": str(fn.get("name") or ""),
                                 "arguments": str(fn.get("arguments") or "")},
                }]})
            u = data.get("usage")
            if isinstance(u, dict):
                usage = u

        # 流结束：flush 两个提取器的残留（截断的 DSML 块补回开标签按正文放行，
        # 截断的 thought 按 reasoning 放行 —— 都不吞内容）
        for extractor, is_reasoning in ((content_ex, False), (reasoning_ex, True)):
            rest = extractor.flush()
            if rest.get("text"):
                yield (_emit_reasoning(rest["text"]) if is_reasoning
                       else _emit_text(rest["text"]))
            if rest.get("reasoning"):
                yield _emit_reasoning(rest["reasoning"])
        if usage:
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            details = usage.get("prompt_tokens_details") if isinstance(
                usage.get("prompt_tokens_details"), dict) else {}
            cached = int(details.get("cached_tokens")
                         or usage.get("prompt_cache_hit_tokens") or 0)
            out_usage = {"prompt_tokens": pt, "completion_tokens": ct,
                         "total_tokens": int(usage.get("total_tokens") or pt + ct)}
            if cached:
                out_usage["prompt_tokens_details"] = {"cached_tokens": cached}
            cdetails = usage.get("completion_tokens_details")
            if isinstance(cdetails, dict):
                out_usage["completion_tokens_details"] = cdetails
            yield {"id": chunk_meta["id"], "object": "chat.completion.chunk",
                   "created": chunk_meta["created"], "model": chunk_meta["model"],
                   "choices": [], "usage": out_usage}
        # finish 帧：finish_reason='length' 必须优先于 tool_calls ——
        # 先看 tool_calls 会让截断的参数被当成完整调用执行
        if finish_reason == "length":
            reason = "length"
        elif finish_reason == "tool_calls" or tool_index > 0:
            reason = "tool_calls"
        else:
            reason = finish_reason or "stop"
        yield _chunk({}, finish=reason)

    # ── 非流式：聚合自身流 ───────────────────────────
    async def chat_completion(self, request, api_key, base_url,
                              extra_headers=None) -> dict:
        """上游只有 SSE → 聚合自身流为一次性响应。"""
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: Dict[int, dict] = {}
        usage: dict = {}
        finish = None
        model_name = str(getattr(request, "model", "") or "")
        async for chunk in self.stream_chat_completion(request, api_key, base_url,
                                                       extra_headers):
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
                for tc in (delta.get("tool_calls") or []):
                    idx = int(tc.get("index") or 0)
                    slot = tool_calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    # 空名分片不能覆盖首个分片解析出的真实工具名
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    slot["function"]["arguments"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        message: dict = {"role": "assistant",
                         "content": "".join(content_parts) or None}
        if reasoning_parts:
            # 与请求侧对称：assistant 消息保留 reasoning_content（回放不丢思考）
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
            for slot in message["tool_calls"]:
                slot["function"]["arguments"] = ca.normalize_tool_arguments(
                    slot["function"]["arguments"])
        return {
            "id": f"chatcmpl-codearts-{ca.new_chat_id()[:20]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish or "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0,
                               "total_tokens": 0},
        }

    # ── 模型目录 ─────────────────────────────────────
    async def list_models(self, api_key, base_url, extra_headers=None) -> List[ModelInfo]:
        """主入口，**永不抛异常**（照仓库 qoder/health 的做法）。

        `_list_models_impl` 内部已对在线目录失败做了静态种子兜底，这里的
        外层 try 只兜「兜底逻辑本身也炸」的意外（如 ModelInfo 构造被污染）。
        """
        try:
            return await self._list_models_impl(api_key, base_url, extra_headers)
        except Exception as e:
            logger.warning("codearts list_models failed: %s: %s",
                           type(e).__name__, str(e)[:160])
            return []

    async def _list_models_impl(self, api_key, base_url,
                               extra_headers=None) -> List[ModelInfo]:
        """双端点在线目录（gateway benefit + snap builtin）合并；失败回退静态种子。"""
        cred = self._resolve_credentials(api_key)
        if cred.get("_error") and not cred.get("access_key_id"):
            # 凭据本身不可用：不冒充有模型（集成层据此把连接标记为需重登）
            logger.warning("codearts list_models: %s", cred["_error"])
            return []
        models: List[dict] = []
        try:
            models = await ca.fetch_remote_models(cred)
        except Exception as e:
            logger.warning("codearts fetch_remote_models failed: %s", e)
            models = []
        source = models or ca.static_model_seed()
        out: List[ModelInfo] = []
        seen: set = set()
        for m in source:
            mid = str(m.get("id") or "")
            if not mid or mid in seen or ca.is_vl_model(mid):
                continue
            seen.add(mid)
            out.append(ModelInfo(
                model_id=mid,
                display_name=str(m.get("name") or mid),
                is_free=ca.is_benefit_model(mid),
                supports_streaming=True,
                supports_vision=False,   # 端点拒绝非 text 块，VL 模型已被过滤
                input_modalities=["text"],
                context_length=ca.model_context_window(mid) or 4096,
            ))
        return out

    # ── 健康探测（签名拉模型目录：轻量且必须验签）──────────
    async def health_check(self, model, api_key, base_url,
                           extra_headers=None, timeout: int = 10) -> HealthResult:
        """探活：签名拉一次模型目录。**永不抛异常**（健康探测的调用方不 try）。"""
        start = time.time()
        try:
            return await self._health_check_impl(model, api_key, timeout, start)
        except Exception as e:
            return HealthResult(status="unhealthy",
                                latency_ms=(time.time() - start) * 1000,
                                error_message=f"{type(e).__name__}: {str(e)[:160]}")

    async def _health_check_impl(self, model, api_key, timeout, start
                                 ) -> HealthResult:
        cred = self._resolve_credentials(api_key)
        if cred.get("_error") and not cred.get("access_key_id"):
            return HealthResult(status="unhealthy",
                                latency_ms=(time.time() - start) * 1000,
                                error_message=cred["_error"][:180])
        models = await ca.fetch_remote_models(cred)
        if not models:
            # 目录拉不到：签名/网络可能有问题时不要谎报 healthy
            return HealthResult(status="degraded",
                                latency_ms=(time.time() - start) * 1000,
                                error_message="模型目录拉取失败（签名或网络）")
        ids = {m.get("id") for m in models}
        ok = (not model) or (model in ids)
        return HealthResult(
            status="healthy" if ok else "degraded",
            latency_ms=(time.time() - start) * 1000,
            error_message="" if ok else f"model {model} not in catalog")
