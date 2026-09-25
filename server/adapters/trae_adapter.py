"""TraeAdapter — 把 OpenAI 格式请求代理到 Trae（字节跳动）的 SOLO 通道。

端口自 Jet-Hub 的 `src/trae-adapter.ts`（1396 行）+ `src/trae.ts`。
与 OpenAI 兼容适配器的差异（每一项都是实测结论，见 `server/core/trae.py`）：

- **请求与响应都要转换**：请求体走 `transform_to_solo_body`（function /
  config_name / tools 参数 JSON 字符串化），响应是 SOLO 自定义 SSE
  （`output` / `token_usage` / `done` / `error`），需转成 OpenAI chunk。
- **模型按通道路由**：同一模型只在**列出它的** `function` 里可调用，
  发错通道会得到**流内 4001**（HTTP 仍是 200）。故发送前查模型所属通道。
- **凭据带机器指纹**：`machine_id` / `device_id` 存在连接记录的 scope 列
  （登录时生成并持久化），与 access_token 一起用于请求头。
- **HTTP 200 零事件只重试一次**，且只在**首个上游事件之前**（一旦有
  output / usage / tool_calls 就绝不重放 —— 重放会重复计费并可能重复执行工具）。
- **历史超 ~480K 字符自动裁剪**（上游超限会静默断流，不发错误码）。

⚠️ **依赖 CN IDE 加密信封的模型不可用**：真实 CN IDE 的 `llm_utils_chat`
请求体是**加密的**（配 `x-helios` / `x-medusa` / `x-neptune` / `x-request-pin` /
`x-requested-at`），Jet-Hub 与本实现**都未实现**该加密。本适配器走旧 SOLO
明文协议（`solo_work_lite` 等通道）；`deepseek-v4-flash` 等实际走加密通道的
模型会失败。清单见 `TRAE_ENCRYPTED_ONLY_MODELS`（**待复测**，不要据此删模型）。
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
from server.core import trae as tr

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 600

# 已知**必须**走加密信封、因本实现未加密而不可用的模型。
#
# ⚠️ 这是**待复测**的暂定清单，来源是 Jet-Hub 的抓包记录（`docs/agents/trae.md`）：
# 该文件明确写过「仅换版本头解不开加密的那批模型（`deepseek-v4-flash` 等仍失败）」。
# Jet-Hub 反复强调过一件事：**把某一刻的快照写成判据会让后人误删可用模型**
# （`is_custom_model` 名单就整体失效过）。故本表只用于在显示名上加「暂不可用」
# 标记，**不用于过滤** —— 模型仍在目录里、仍可被点名调用，失败时由上游如实报错。
TRAE_ENCRYPTED_ONLY_MODELS = ("deepseek-v4-flash",)

# 目录级「加密未实现」提示只记一次（每次拉目录都记会刷屏）
_ENCRYPTION_NOTED = False

_CATALOG: Dict[str, tuple] = {}      # key -> (expires_monotonic, catalog dict)
_INFLIGHT: Dict[str, asyncio.Task] = {}
# token 指纹 -> (expires_monotonic, meta)；身份字段（machine_id/device_id/uid）
# 在连接记录里不会变，缓存只为省掉每次请求的 DB 扫描 + 解密
_META: Dict[str, tuple] = {}
_META_TTL_SECONDS = 300


def _token_key(token: str) -> str:
    return hashlib.sha256(f"trae:{token}".encode()).hexdigest()[:24]


def _domain_of(base_url: str) -> str:
    """从 base_url 判国内版 / 国际版（两版**仅域名不同**，协议一致）。"""
    host = ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url or "").netloc or "").lower()
    except ValueError:
        host = ""
    if "trae.ai" in host or "alisg" in host:
        return "trae.ai"
    return ""


def _agent_host(base_url: str, domain: str) -> str:
    """Agent 基址：优先采信调用方给的 base_url 的 origin，否则用产品默认值。"""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url or "")
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        pass
    return tr.product_for(domain)["agent_host"]


def _clean_body(body: dict) -> dict:
    """剔掉 OpenAI 请求体里的空值与 AIGate 内部字段。

    SOLO 上游对多余/空值字段敏感（`temperature: null` 之类没有依据的字段
    不该发）。`extra` 是 AIGate 自己的容器，不属于上游协议。
    """
    out = {}
    for key, value in (body or {}).items():
        if key == "extra" or value is None:
            continue
        out[key] = value
    return out


class TraeAdapter(BaseAdapter):
    """Trae（字节跳动）SOLO 通道适配器。"""

    def __init__(self, timeout: Optional[int] = None):
        self._connect_timeout = 30.0
        self._read_timeout = float(timeout) if timeout else 300.0

    # ── 凭据与身份 ───────────────────────────────────
    async def _resolve_meta(self, api_key: str) -> dict:
        """取该 token 所属连接记录里的身份字段（machine_id / device_id / uid）。

        `machine_id` / `device_id` **不在 token 里**，它们由登录流程生成并写进
        连接记录的 scope 列 —— 必须随每次请求下发，且**永不重新生成**
        （上游按 machine_id 标识设备，换值可能触发风控或要求重新登录）。

        按 token 匹配连接（而非固定取 `__default`）：多账号时拿错身份会把
        请求打成「异常设备」。匹配不到（如手工导入的裸 token）回退默认账号，
        并**如实告警**而不是伪造身份。
        """
        token = str(api_key or "")
        key = _token_key(token)
        now = time.monotonic()
        hit = _META.get(key)
        if hit and hit[0] > now:
            return hit[1]
        meta: dict = {}
        try:
            from sqlalchemy import select
            from server.db import AsyncSessionLocal
            from server.models.oauth_token import OAuthToken
            from server.core.oauth_client import get_oauth_client
            client = get_oauth_client()
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(OAuthToken).where(
                        OAuthToken.provider_code.in_(("trae", "trae_intl")),
                        OAuthToken.is_active.is_(True),
                    ).order_by(OAuthToken.id)
                )).scalars().all()
                matched = None
                for row in rows:
                    try:
                        if client._crypto.decrypt(row.access_token_enc) == token:
                            matched = row
                            break
                    except Exception:
                        continue
                if matched is None:
                    meta = await client.get_token_meta("trae", db)
                elif matched.scope:
                    parsed = json.loads(matched.scope)
                    if isinstance(parsed, dict):
                        meta = parsed
                    if matched.provider_code == "trae_intl":
                        meta.setdefault("domain", "trae.ai")
        except Exception as e:
            logger.warning("trae meta resolve failed: %s", e)
            meta = {}
        _META[key] = (now + _META_TTL_SECONDS, meta)
        return meta

    def _credential(self, api_key: str, meta: dict, domain: str) -> dict:
        """组装请求头所需的凭据视图。缺 uid 直接报错（X-Uid 是必填头）。"""
        uid = str(meta.get("uid") or "")
        if not uid:
            raise RuntimeError(
                "trae 凭证缺少 uid（请重新连接 Trae 账号；身份字段随登录持久化）")
        machine_id = str(meta.get("machine_id") or "")
        device_id = str(meta.get("device_id") or "")
        if not machine_id or not device_id:
            # 不阻断：头构造会省略这两个字段（上游可能仍接受），但必须留痕
            logger.warning("trae 凭证缺少 machine_id/device_id（登录时未落库；"
                           "上游可能判定为异常设备）")
        return {
            "access_token": str(api_key or ""),
            "uid": uid,
            "machine_id": machine_id,
            "device_id": device_id,
            "domain": str(meta.get("domain") or domain or ""),
        }

    # ── 模型目录 ─────────────────────────────────────
    def _cache_key(self, cred: dict) -> str:
        seed = cred.get("uid") or cred.get("access_token") or "anon"
        return hashlib.sha256(f"trae-catalog:{seed}".encode()).hexdigest()[:24]

    async def _catalog(self, cred: dict, base_url: str = "",
                       force: bool = False) -> Optional[dict]:
        """拉取远端多通道模型目录（带 TTL 缓存 + 并发合流）。

        ⚠️ 必须用 **batch** 端点：一次传全部 22 个 function，响应里每个通道
        各自一套目录。只传几个聊天通道会让条目错位甚至被解析器跳过。
        """
        key = self._cache_key(cred)
        now = time.monotonic()
        hit = _CATALOG.get(key)
        if hit and hit[0] > now and not force:
            return hit[1]
        inflight = _INFLIGHT.get(key)
        if inflight and not force:
            return await asyncio.shield(inflight)

        domain = cred.get("domain", "")

        async def _fetch():
            models = await tr.fetch_models(cred, domain)
            if not models:
                return None
            result = {"models": models,
                      "by_id": {m["id"]: m for m in models}}
            _CATALOG[key] = (time.monotonic() + CATALOG_TTL_SECONDS, result)
            return result

        task = asyncio.get_event_loop().create_task(_fetch())
        _INFLIGHT[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if _INFLIGHT.get(key) is task:
                _INFLIGHT.pop(key, None)

    def _channel_for(self, catalog: Optional[dict], model: str) -> str:
        """该模型所属的聊天通道（`function`）。

        ⚠️ **模型只在列出它的通道里可调用**：发错通道上游会回流内
        `code=4001 param is invalid`（实测 `glm-5.1` 在 `solo_work_lite` 报错、
        在 `solo_agent_remote` 正常；`glm-5-turbo` 恰好相反）。
        查不到该模型的通道时回退默认通道 `solo_work_lite`。
        """
        entry = ((catalog or {}).get("by_id") or {}).get(model) or {}
        channel = str(entry.get("channel") or "")
        return channel or tr.TRAE_DEFAULT_FUNCTION

    # ── SSE 消费（SOLO → OpenAI chunk）──────────────
    class _SoloStream:
        """SOLO 事件流 → OpenAI chunk（含 finish/usage 合流与空响应标记）。

        SOLO 事件格式（照 Jet-Hub `solosse.go` 的实测记录）：
            event:output        data:{"response":"…","reasoning_content":"…","tool_calls":…}
            event:token_usage   data:{"prompt_tokens":21,"completion_tokens":142,…}
            event:done          data:{"finish_reason":"stop"}
            event:error         data:{"code":4008,"message":"quota exceeded"}

        解析须兼容 `data: {...}` 与 `data:{...}`（实测无空格），且 data 可能
        跨行拼接（故累积到空行才消费）。
        """

        def __init__(self, model: str):
            self.model = model
            self.chunk_id = f"chatcmpl-trae-{uuid.uuid4().hex[:20]}"
            self.created = int(time.time())
            self.pending_usage: Optional[dict] = None
            self.finish_reason: Optional[str] = None
            self.role_sent = False
            self.terminal = False
            # 是否收到过**任何**可解析的上游事件 —— 空响应重试的唯一依据
            self.saw_any_event = False
            self.error: Optional[str] = None
            self.tool_calls: Dict[int, dict] = {}

        # ── chunk 构造 ──
        def _chunk(self, delta: dict, finish_reason=None, usage=None) -> dict:
            choice = {"index": 0, "delta": delta, "finish_reason": finish_reason}
            chunk = {"id": self.chunk_id, "object": "chat.completion.chunk",
                     "created": self.created, "model": self.model,
                     "choices": [choice]}
            if usage is not None:
                chunk["usage"] = usage
            return chunk

        def _terminal_chunk(self) -> dict:
            """终结 chunk：带 finish_reason，usage 有则一并带上。"""
            usage = self.pending_usage
            finish = self.finish_reason or "stop"
            self.pending_usage = None
            self.finish_reason = None
            return self._chunk({}, finish_reason=finish, usage=usage)

        def _tool_delta(self, calls: list) -> list:
            """归一化 SOLO tool_calls 为 OpenAI delta 形态（补 index/type）。"""
            out = []
            for i, call in enumerate(calls or []):
                if not isinstance(call, dict):
                    continue
                item = dict(call)
                raw_index = item.get("index")
                index = int(raw_index) if isinstance(raw_index, int) else i
                item["index"] = index
                item.setdefault("type", "function")
                fn = item.get("function")
                if isinstance(fn, dict):
                    slot = self.tool_calls.setdefault(index, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
                    if item.get("id"):
                        slot["id"] = item["id"]
                    # name 按**赋值**（SOLO 一次给全名，累加会重复）；
                    # arguments 按**累加**（分片下发）
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += str(fn["arguments"])
                out.append(item)
            return out

        # ── 单条事件 → chunks ──
        def handle(self, event_name: str, data: str) -> List[dict]:
            if self.terminal:
                return []
            ev = tr.parse_sse_event(event_name, data)
            if ev is None:
                return []
            # 任何**可解析**的事件（含 metadata / timing_cost）都算「上游确实
            # 开工了」—— 此后绝不重放请求（见 saw_any_event）
            self.saw_any_event = True
            name = ev.get("event")
            if name == "output":
                return self._handle_output(ev)
            if name == "token_usage":
                if isinstance(ev.get("usage"), dict):
                    self.pending_usage = tr.normalize_usage(ev["usage"])
                return []
            if name == "done":
                self.finish_reason = ev.get("finish_reason") or "stop"
                self.terminal = True
                return [self._terminal_chunk()]
            if name == "error":
                code = ev.get("error_code", -1)
                message = ev.get("error_message") or "unknown error"
                self.error = tr.solo_error_message(code, message, self.model)
                self.terminal = True
                return [{"error": self.error}]
            # metadata / timing_cost / extra_info：只标记「开工」，不产出
            return []

        def _handle_output(self, ev: dict) -> List[dict]:
            delta: dict = {}
            # role 只在首个实质 chunk 上带一次（OpenAI 客户端据此初始化消息）
            if not self.role_sent:
                delta["role"] = "assistant"
            content = ev.get("response")
            if isinstance(content, str) and content:
                delta["content"] = content
            reasoning = ev.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                delta["reasoning_content"] = reasoning
            calls = ev.get("tool_calls")
            if calls:
                delta["tool_calls"] = self._tool_delta(calls)
            if len(delta) == 1 and "role" in delta:
                # 只有 role 没有内容：不单独发（避免产生一个空 chunk），
                # 但保留 role_sent=False 以便下一条实质 chunk 补上 role
                return []
            self.role_sent = True
            return [self._chunk(delta)]

        def flush(self) -> List[dict]:
            """流结束时补发 finish chunk（上游没发 `done` 也要给出终结）。

            上游有时在 `done` 之前就把连接关掉 —— 不给 finish 会让客户端
            一直等（AIGate 侧也会认为流异常结束）。
            """
            if self.terminal or not self.saw_any_event:
                return []
            self.terminal = True
            if self.finish_reason is None:
                self.finish_reason = "tool_calls" if self.tool_calls else "stop"
            return [self._terminal_chunk()]

    # ── 核心流式调用 ─────────────────────────────────
    async def stream_chat_completion(self, request, api_key, base_url,
                                     extra_headers=None) -> AsyncGenerator[dict, None]:
        body = request.model_dump() if hasattr(request, "model_dump") else dict(request)
        model = str(body.get("model") or "")
        domain = _domain_of(base_url)
        meta = await self._resolve_meta(api_key)
        cred = self._credential(api_key, meta, domain)
        catalog = await self._catalog(cred, base_url)
        channel = self._channel_for(catalog, model)

        # ⚠️ tools / tool_choice 必须放进**源 OpenAI 对象**里再交给转换函数：
        # SOLO 要求 `function.parameters` 是 JSON 字符串（OpenAI 标准是 object），
        # 那一步序列化只对**转换时已存在**的 tools 生效。转换后再补 `body["tools"]`
        # 会保持 object 形态发给上游被拒（且错误信息不会指向这里）。
        clean = _clean_body(body)
        clean["model"] = model
        # 历史裁剪：超 ~480K 字符上游会**静默断流**（不发错误码、流就那么断掉）。
        # 必须在转换**之前**裁剪（裁的是 OpenAI wire 消息），且不切断工具配对。
        if isinstance(clean.get("messages"), list):
            clean["messages"] = tr.trim_history(clean["messages"])
        if clean.get("max_tokens") is not None:
            clean["max_tokens"] = tr.clamp_max_tokens(clean["max_tokens"])
        payload = tr.transform_to_solo_body(clean, "", channel)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = tr.solo_headers(cred, True, cred.get("domain", ""))
        url = f"{_agent_host(base_url, domain)}{tr.TRAE_CHAT_PATH}"

        timeout = httpx.Timeout(self._connect_timeout, read=self._read_timeout)
        for attempt in range(2):
            st = self._SoloStream(model)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers,
                                         content=raw) as resp:
                    if resp.status_code != 200:
                        detail = ""
                        try:
                            detail = (await resp.aread()).decode("utf-8", "replace")[:200]
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"trae http {resp.status_code}: {detail}")
                    async for chunk in self._consume(resp, st):
                        yield chunk
            if st.saw_any_event or st.error:
                return
            # ── 空响应（HTTP 200 但一个事件都没发）**重试一次** ──
            # 只在「一个上游事件都没收到」时才走到这里：一旦有 output / usage /
            # tool_calls，`saw_any_event` 已置位，重放会让上游**重复计费并可能
            # 重复执行工具**。故这里绝不重放已开工的请求。
            if attempt == 0:
                logger.warning("trae: 上游返回空响应（零事件），重试一次")
                continue
        if not st.saw_any_event:
            raise RuntimeError("trae: upstream returned no events "
                               "(empty response before first model event)")

    async def _consume(self, resp, st: "TraeAdapter._SoloStream"):
        """解析上游 SSE 行 → 逐个产出 OpenAI chunk。"""
        current_event = ""
        current_data = ""
        async for line in resp.aiter_lines():
            if st.terminal:
                break
            if not line.strip():
                # 空行 = 事件边界
                if current_event:
                    for chunk in st.handle(current_event, current_data):
                        yield chunk
                    current_event = ""
                    current_data = ""
                continue
            stripped = line.strip()
            if stripped.startswith("event:"):
                current_event = stripped[len("event:"):].strip()
            elif stripped.startswith("data:"):
                # data 可能跨行拼接；兼容 `data:{…}`（实测无空格）
                current_data += stripped[len("data:"):]
            # 注释行（":"）与其它：忽略
        # 上游没发 `done` 就断流 → 补一个终结 chunk，别让客户端一直等
        for chunk in st.flush():
            yield chunk

    # ── 非流式：聚合自身流（SOLO 端点只有 SSE） ─────────
    async def chat_completion(self, request, api_key, base_url,
                              extra_headers=None) -> dict:
        model_name = str(getattr(request, "model", "") or "")
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: Dict[int, dict] = {}
        finish = None
        usage: dict = {}
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
            "id": f"chatcmpl-trae-{uuid.uuid4().hex[:20]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish or "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0,
                               "total_tokens": 0},
        }

    # ── 模型目录 ─────────────────────────────────────
    @staticmethod
    def display_name_for(model: dict) -> str:
        """展示名：`模型名 · 倍率`（活动期 `x原价→x折后价`）。

        ⚠️ `credits_rate == 0` 是**合法值（免费）**，必须与「没有倍率信息」
        区分开 —— 用 `> 0` 过滤会恰好漏掉用户最关心的免费模型。
        没有倍率信息时只显示模型名（**不编造** `x1`）。

        依赖加密信封的模型（`TRAE_ENCRYPTED_ONLY_MODELS`）追加「暂不可用」标记
        —— 如实标注，不静默列出一个注定失败的模型。
        """
        name = str(model.get("name") or model.get("id") or "")
        rate = model.get("credits_rate")
        if isinstance(rate, (int, float)) and not isinstance(rate, bool):
            current = "免费" if rate == 0 else f"x{rate}"
            original = model.get("original_credits_rate")
            if (isinstance(original, (int, float)) and not isinstance(original, bool)
                    and original > rate):
                name = f"{name} · x{original}→{current}"
            else:
                name = f"{name} · {current}"
        if str(model.get("id") or "") in TRAE_ENCRYPTED_ONLY_MODELS:
            name = f"{name} · 暂不可用（CN 加密信封未实现）"
        return name

    def _to_model_info(self, model: dict) -> ModelInfo:
        rate = model.get("credits_rate")
        rate = float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) else None
        # 图片能力**逐模型**判定（远端 `display_config.multimodal`）：这是
        # AIGate 的准入闸门 —— 声明 image 才允许附件，未声明按不支持（保守）。
        modalities = ["text", "image"] if model.get("multimodal") is True else ["text"]
        reasoning = model.get("reasoning_config") or {}
        supports_reasoning = None
        if reasoning and reasoning.get("support_thinking") is not False:
            supports_reasoning = bool(reasoning.get("options"))
        return ModelInfo(
            model_id=str(model["id"]),
            display_name=self.display_name_for(model),
            # 订阅制上游无 USD 单价；倍率是唯一的「价格」口径（同 Qoder）
            is_free=(rate == 0.0),
            input_price=0.0, output_price=0.0,
            supports_streaming=True,
            supports_vision=(model.get("multimodal") is True),
            supports_reasoning_effort=supports_reasoning,
            context_length=int(model.get("context_window") or 200_000),
            input_modalities=modalities,
            max_output_tokens=model.get("max_output_tokens"),
            price_ratio=rate,
        )

    async def list_models(self, api_key, base_url,
                          extra_headers=None) -> List[ModelInfo]:
        """远端多通道目录 → ModelInfo 列表。

        过滤分两层（与 Jet-Hub 一致）：
        1. **解析层**（`trae.parse_batch_model_list`）硬性剔除
           `usage != chat_completion` / `config_switch == false` /
           `is_invisible_to_user == true`；
        2. **运行时**（这里）剔除 `is_custom_model == true` —— 那是「需在
           IDE 内自行绑定供应商」的模型，必然回流内 `4001`。

        拉取失败时抛错（由 model_catalog 回退注册表静态种子）。
        """
        meta = await self._resolve_meta(api_key)
        cred = self._credential(api_key, meta, _domain_of(base_url))
        catalog = await self._catalog(cred, base_url)
        if not catalog:
            raise RuntimeError("trae 模型目录拉取失败（检查连接/网络）")
        self._note_encryption_once(len(catalog["models"]))
        out = []
        for model in catalog["models"]:
            if model.get("is_custom_model") is True:
                continue
            out.append(self._to_model_info(model))
        return out

    @staticmethod
    def _note_encryption_once(count: int) -> None:
        """目录级「加密未实现」提示只记一次（避免每次刷目录都刷屏）。"""
        global _ENCRYPTION_NOTED
        if _ENCRYPTION_NOTED:
            return
        _ENCRYPTION_NOTED = True
        logger.warning(
            "trae: 已加载 %d 个可调用模型（走旧 SOLO 明文协议）。"
            "⚠️ 真实 CN IDE 的 llm_utils_chat 请求体是加密的"
            "（x-helios/x-medusa/x-neptune/x-request-pin/x-requested-at），"
            "本实现与 Jet-Hub 均未实现该加密 → 依赖它的模型"
            "（如 deepseek-v4-flash）不可用。", count)

    # ── 健康探测（拉目录，轻量且带鉴权） ───────────────
    async def health_check(self, model, api_key, base_url,
                           extra_headers=None, timeout: int = 10) -> HealthResult:
        start = time.time()
        try:
            meta = await self._resolve_meta(api_key)
            cred = self._credential(api_key, meta, _domain_of(base_url))
            catalog = await self._catalog(cred, base_url, force=False)
            if catalog is None:
                return HealthResult(
                    status="unhealthy",
                    latency_ms=(time.time() - start) * 1000,
                    error_message="trae catalog fetch failed")
            ok = (not model) or (model in (catalog.get("by_id") or {}))
            return HealthResult(
                status="healthy" if ok else "degraded",
                latency_ms=(time.time() - start) * 1000,
                error_message="" if ok else f"model {model} not in catalog")
        except Exception as e:
            return HealthResult(status="unhealthy",
                                latency_ms=(time.time() - start) * 1000,
                                error_message=str(e)[:180])

    # ── 额度/签到（供 oauth_usage / checkin 集成调用）────
    async def fetch_usage(self, api_key, base_url="") -> dict:
        """积分余额（`ide_user_ent_usage`）；失败返回带 error 的空结构。"""
        meta = await self._resolve_meta(api_key)
        cred = self._credential(api_key, meta, _domain_of(base_url))
        return await tr.fetch_credits(cred, cred.get("domain", ""))

    async def claim_daily(self, api_key, base_url="") -> dict:
        """每日签到（两步：status 预检 → claim → 补查 status）。"""
        meta = await self._resolve_meta(api_key)
        cred = self._credential(api_key, meta, _domain_of(base_url))
        return await tr.claim_checkin(cred, cred.get("domain", ""))
