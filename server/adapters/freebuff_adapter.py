"""FreebuffAdapter — 把 OpenAI 格式请求代理到 Codebuff 免费层（freebuff）。

协议实现见 `server/core/freebuff.py`（双源交叉核对：社区逆向 worker.js +
CodebuffAI/freebuff 官方源码）。与通用 OpenAI 兼容适配器的关键差异：

- **三段式门控**：每次请求都要走 `session → agent-runs → chat`。session 约
  1 小时有效且**创建时扣 Freebucks**（按模型单价）→ 进程内按 (token, model)
  缓存 instanceId，剩 >60s 才复用，否则重建（照 worker.js `isUsableSession`）。
- **上游强制流式**：出站 `stream=true` 恒真；本适配器把上游 SSE 透传给客户端
  （客户端要非流式时由上层聚合，与 CodeBuddy 渠道同款约定）。
- **run 链**：主 run（按模型映射 `base2-free-*`）+ `context-pruner` 子 run。
  实测 run_id 可跨请求复用（上游只校验存在性）→ 进程内 10 分钟缓存省两次调用。
- **工具集伪装 + Buffy 前缀**：见 core/freebuff.py 顶部说明（不做会静默降级模型）。
- **并发纪律**：上游对单账号并发极敏感（429/428/排队超时）→ 每 token 一把
  asyncio.Lock，串行化 session/run/chat 的建立过程。
- **国别分层**：非 US 出口是 limited access（模型目录缩减，仍可用）——上游策略，
  不在这里做任何规避；`/v1/models` 如实列出、失败时由上游如实报错。

⚠️ **单账号单会话**：一个 freebuff 账号同一时间只能一个客户端在线。AIGate 多路
复用同一账号时会互相顶掉 session（上游 409 session_superseded / 428 waiting_room）。
本适配器用 per-token 锁 + instanceId 复用把串扰压到最低，但不做多账号池。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import AsyncGenerator, Dict, List, Optional

import httpx

from server.adapters.base_adapter import BaseAdapter, HealthResult, ModelInfo
from server.core import freebuff as fb

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 300.0

# instanceId 复用：剩 >60s 才复用（照 worker.js isUsableSession）
SESSION_MIN_REMAIN_MS = 60_000
# run 链缓存（worker.js 实测 run_id 可跨请求复用，10 分钟）
RUN_CACHE_TTL = 600.0

_SESSIONS: Dict[str, dict] = {}      # (token_hash, model) -> {instance_id, expires_at_monotonic}
_RUNS: Dict[str, tuple] = {}         # (token_hash, agent) -> (expires_monotonic, run_id, child_run_id)
_LOCKS: Dict[str, asyncio.Lock] = {}  # token_hash -> Lock（单账号串行）
_CATALOG: Dict[str, tuple] = {}      # base_url -> (expires_monotonic, [model ids])


def _tk(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()[:20]


def _lock_for(token: str) -> asyncio.Lock:
    key = _tk(token)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _clean_body(request) -> dict:
    """请求体 → dict：优先 `model_dump(exclude_none=True)`（与 openai_compat 同口径：
    剔除 null、保留 temperature:0 这类合法假值）。"""
    if hasattr(request, "model_dump"):
        try:
            return request.model_dump(exclude_none=True)
        except Exception:
            pass
    return dict(request) if isinstance(request, dict) else {}


class FreebuffAdapter(BaseAdapter):
    def __init__(self, timeout: Optional[int] = None):
        self.timeout = timeout or 180

    # ── 代理 ─────────────────────────────────────────
    def _proxy(self, force: bool = False) -> dict:
        """取代理参数并记录实际使用的出口（供请求日志落库）。

        `force=True`（服务商开启「走代理」开关 → extra_headers.__proxy_force）
        时即使代理池全局关闭也走代理：freebuff 免费层按**出口国别**分层，
        换出口是用户可用的唯一手段（US 出口才有 full access）。
        """
        from server.core.proxy_pool import get_proxy_pool, CURRENT_PROXY_URL
        pk = get_proxy_pool().proxied_kwargs(force=force)
        CURRENT_PROXY_URL.set(pk.get("proxy"))
        return pk

    def _proxy_url(self, force: bool = False) -> Optional[str]:
        try:
            return self._proxy(force=force).get("proxy")
        except Exception:
            return None

    # ── session / run 生命周期 ───────────────────────
    async def _ensure_session(self, token: str, model: str,
                              force_proxy: bool = False) -> dict:
        """取可用 instanceId：缓存剩 >60s 复用，否则 POST 新建（新建会扣 Freebucks）。"""
        key = (_tk(token), model)
        now = time.monotonic()
        hit = _SESSIONS.get(key)
        if hit and hit.get("expires_at_monotonic", 0) - now > SESSION_MIN_REMAIN_MS / 1000:
            return hit
        proxy = self._proxy_url(force=force_proxy)
        sess = await fb.create_session(token, model, proxy=proxy)
        # expires_at 是上游 ISO 或毫秒；解析失败按 50 分钟保守估算
        ttl = 3000.0
        exp = sess.get("expires_at")
        if isinstance(exp, str):
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                ttl = max(60.0, (dt - datetime.now(timezone.utc)).total_seconds())
            except ValueError:
                pass
        elif isinstance(exp, (int, float)) and exp > 1e12:
            ttl = max(60.0, (exp / 1000 - time.time()))
        _SESSIONS[key] = {"instance_id": sess["instance_id"], "model": sess.get("model") or model,
                          "expires_at_monotonic": now + ttl}
        return _SESSIONS[key]

    async def _ensure_run(self, token: str, agent: str,
                          force_proxy: bool = False) -> str:
        """主 run + context-pruner 子 run；10 分钟内复用（上游只校验 run_id 存在）。"""
        key = (_tk(token), agent)
        now = time.monotonic()
        hit = _RUNS.get(key)
        if hit and hit[0] > now:
            return hit[1]
        proxy = self._proxy_url(force=force_proxy)
        run_id = await fb.start_run(token, agent, proxy=proxy)
        # 子 run 失败不阻断（chat 只校验主 run 存在）
        try:
            await fb.start_run(token, fb.CONTEXT_PRUNER_AGENT, [run_id], proxy=proxy)
        except Exception as e:
            logger.debug("freebuff: context-pruner 子 run 失败（忽略）：%s", e)
        _RUNS[key] = (now + RUN_CACHE_TTL, run_id)
        return run_id

    # ── 上游调用 ─────────────────────────────────────
    async def _open_stream(self, body: dict, token: str, model: str,
                           force_proxy: bool = False):
        """建立 session/run 并发起 chat，返回 (client, response)。

        调用方负责关闭 client（async with）。
        重试语义：session 失效（428/409）→ 清缓存重建一次；不重放已出内容的请求。
        """
        proxy = self._proxy_url(force=force_proxy)
        last_err: Optional[Exception] = None
        for attempt in range(2):
            sess = await self._ensure_session(token, model, force_proxy=force_proxy)
            run_id = await self._ensure_run(token, fb.agent_for_model(model),
                                            force_proxy=force_proxy)
            payload = fb.build_chat_payload(body, model, run_id, sess["instance_id"], token)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": fb.SDK_UA,
                "x-freebuff-instance-id": sess["instance_id"],
            }
            timeout = httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT)
            client = httpx.AsyncClient(timeout=timeout, proxy=proxy)
            try:
                req = client.build_request("POST",
                                           f"{fb.FREEBUFF_API_BASE}/api/v1/chat/completions",
                                           headers=headers, content=raw)
                resp = await client.send(req, stream=True)
            except Exception as e:
                await client.aclose()
                last_err = RuntimeError(f"freebuff chat 网络错误：{type(e).__name__}: {str(e)[:150]}")
                continue
            if resp.status_code == 200:
                return client, resp
            # 读错误体后关闭
            try:
                detail = (await resp.aread()).decode("utf-8", "replace")[:400]
            except Exception:
                detail = ""
            await resp.aclose()
            await client.aclose()
            stale = resp.status_code in (428, 409) or "waiting_room" in detail or "session_superseded" in detail
            if stale and attempt == 0:
                # 清掉缓存 session，重建后重试一次（照 worker.js staleSession 分支）
                _SESSIONS.pop((_tk(token), model), None)
                logger.info("freebuff: session 失效（HTTP %s），重建重试一次", resp.status_code)
                continue
            if resp.status_code == 410 or "model_unavailable" in detail:
                raise RuntimeError(f"freebuff 模型已下线：{detail[:200]}")
            if resp.status_code == 429:
                raise RuntimeError(f"freebuff Freebucks 额度已用尽：{detail[:200]}")
            if resp.status_code == 403 and "banned" in detail:
                raise RuntimeError(f"freebuff 账号已被封禁（不可恢复）：{detail[:200]}")
            raise RuntimeError(f"freebuff chat HTTP {resp.status_code}: {detail[:300]}")
        if last_err:
            raise last_err
        raise RuntimeError("freebuff chat: 重试后仍未成功")

    # ── 流式 ─────────────────────────────────────────
    async def stream_chat_completion(self, request, api_key, base_url,
                                     extra_headers=None) -> AsyncGenerator[dict, None]:
        body = _clean_body(request)
        model = str(body.get("model") or "")
        if not model:
            yield {"error": "freebuff: 请求缺少 model"}
            return
        async with _lock_for(api_key):
            # 锁内建立会话（单账号串行，避免并发顶掉 session）
            try:
                client, resp = await self._open_stream(
                    body, api_key, model,
                    force_proxy=bool((extra_headers or {}).get("__proxy_force")))
            except Exception as e:
                yield {"error": f"{type(e).__name__}: {str(e)[:400]}"}
                return
        # 锁外消费流（长流不能占着锁把同账号其他请求饿死；
        # 但 session 建立已串行化，chat 流本身是只读消费）
        try:
            async for line in resp.aiter_lines():
                obj = fb.parse_sse_data(line)
                if obj is None:
                    continue
                fb.restore_tool_names(obj)
                yield obj
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass

    # ── 非流式（聚合流式）─────────────────────────────
    async def chat_completion(self, request, api_key, base_url,
                              extra_headers=None) -> dict:
        model_name = str(getattr(request, "model", "") or "")
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: Dict[int, dict] = {}
        finish = None
        usage: dict = {}
        chunk_id = None
        async for chunk in self.stream_chat_completion(request, api_key, base_url, extra_headers):
            if isinstance(chunk, dict) and chunk.get("error"):
                raise RuntimeError(str(chunk["error"]))
            if isinstance(chunk, dict) and chunk.get("usage"):
                usage = chunk["usage"]
            if isinstance(chunk, dict) and chunk.get("id"):
                chunk_id = chunk["id"]
            for choice in (chunk.get("choices") or []):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index")
                    idx = int(idx) if isinstance(idx, int) else 0
                    slot = tool_calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += str(fn["arguments"])
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
            "id": chunk_id or f"chatcmpl-freebuff-{uuid.uuid4().hex[:20]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish or "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0,
                               "total_tokens": 0},
        }

    # ── 模型目录 ─────────────────────────────────────
    async def _model_ids(self, force: bool = False) -> List[str]:
        """在线目录（社区 releases 资产）→ 失败回退内置兜底表。

        不做上游 /v1/models 探测：freebuff 官方**没有**模型列表端点（模型表在
        客户端源码常量里），且 GET /session 探测会占用 session、顶掉正在进行的
        chat（worker.js 明确警告）。故用社区维护的 releases 资产 + 内置兜底。
        """
        key = "freebuff"
        now = time.monotonic()
        if not force:
            hit = _CATALOG.get(key)
            if hit and hit[0] > now:
                return hit[1]
        ids = await fb.fetch_release_models(self._proxy_url())
        if not ids:
            ids = [mid for mid, _ in fb.FALLBACK_MODELS]
        if ids:
            _CATALOG[key] = (now + 600.0, ids)
        return ids

    async def list_models(self, api_key, base_url,
                          extra_headers=None) -> List[ModelInfo]:
        ids = await self._model_ids()
        display = dict(fb.FALLBACK_MODELS)
        out = []
        for mid in ids:
            out.append(ModelInfo(
                model_id=mid,
                display_name=display.get(mid, mid),
                # Freebucks 按次扣费（非 token 计费）→ 无 USD 单价，全部标免费
                is_free=True,
                input_price=0.0, output_price=0.0,
                supports_streaming=True,
                supports_vision=False,
                supports_reasoning_effort=None,
                context_length=200_000,
                input_modalities=["text"],
            ))
        return out

    # ── 健康探测 ─────────────────────────────────────
    async def health_check(self, model, api_key, base_url,
                           extra_headers=None, timeout: int = 10) -> HealthResult:
        """只读快照探测（GET /session，0 消耗、不创建 session）。

        照 worker.js 的判定语义：200/404=存活；401=token 失效；403+banned=封号；
        429=额度用完。**绝不 POST**（POST 会扣 Freebucks）。
        """
        t0 = time.time()
        try:
            snap, err = await fb.query_snapshot(
                api_key, proxy=self._proxy_url(bool((extra_headers or {}).get("__proxy_force"))))
        except Exception as e:
            return HealthResult(status="unhealthy", latency_ms=0.0,
                                error_message=f"{type(e).__name__}: {str(e)[:150]}")
        latency = (time.time() - t0) * 1000
        if snap is None:
            return HealthResult(status="unhealthy", latency_ms=latency,
                                error_message=(err or "查询失败")[:200])
        code = snap["status_code"]
        data = snap["data"] if isinstance(snap["data"], dict) else {}
        if code == 401:
            return HealthResult(status="unhealthy", latency_ms=latency,
                                error_message="authToken 无效或已被撤销")
        if code == 403:
            st = data.get("status")
            if st == "banned":
                return HealthResult(status="unhealthy", latency_ms=latency,
                                    error_message="账号已被封禁（Terminal，不可恢复）")
            if st == "country_blocked":
                return HealthResult(status="unhealthy", latency_ms=latency,
                                    error_message=f"地区受限（{data.get('countryCode') or 'UNKNOWN'}）")
            return HealthResult(status="degraded", latency_ms=latency,
                                error_message=f"HTTP 403: {str(data)[:120]}")
        if code == 429 or data.get("status") == "rate_limited":
            return HealthResult(status="rate_limited", latency_ms=latency,
                                error_message="Freebucks 额度已用尽")
        return HealthResult(status="healthy", latency_ms=latency)
