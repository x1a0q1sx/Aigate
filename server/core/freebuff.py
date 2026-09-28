"""Freebuff（Codebuff 免费层）协议模块。

协议来源（双源交叉核对，均为实证代码而非文档）：
  1. pingmike2/freebuff2api-wokers 的 worker.js / server.js / freebuff_tools
     （社区逆向实现，AGPL-3.0，含 2026-09 的实测记录）
  2. CodebuffAI/freebuff 官方仓库（CLI/SDK/common 源码，MIT）
     —— free-agents.ts / freebuff-session.ts / freebuff-streak.ts / freebuff-countries.ts

## 上游门控（不是"拿 token 直接调 chat"）

    session(开) → agent-runs(主 + context-pruner 子 run) → chat/completions

  - session：POST /api/v1/freebuff/session 带 x-freebuff-model 拿 instanceId；
    一个 session 约 1 小时，**创建时扣 Freebucks**（按模型单价），复用不扣。
  - agent-runs：START 主 agent（按模型映射 base2-free-*）+ context-pruner 子 run。
    chat 会校验 run_id 存在，缺了 4xx。
  - chat：POST /api/v1/chat/completions，**上游强制流式**（stream 恒为 true），
    非流式客户端由网关聚合 SSE。

## 三条「不说就静默降级」的硬约束（全部有官方源码依据）

1. **Buffy 前缀**：system 消息必须以 `You are Buffy, the strategic coding assistant.`
   **字节级开头**（服务端 hasFreebuffRootSystemPromptOpening 校验）；缺了会 403
   free_mode_cli_required。旧的 `[System Override...]` 前缀绕过已被官方修补。
2. **外域客户端指纹**（2026-09-17/18 官方两轮升级）：tools 里出现 Claude Code /
   Codex / OpenClaw / opencode 等外域 harness 的**精确工具名**，或 system 命中
   harness 身份短语 → **静默降级到 inclusionai/ling-3.0-tiny:free**（不报错、只换
   模型）。对策（照 worker.js）：客户端工具名统一加 `mcp__` 前缀（官方认可
   `server__tool` 形态，且 isUnrecognisedToolName 把含 "__" 的名字排除在观测名单
   外）+ 注入官方 `decide` 作为 genuine 签名工具 + system 里的 harness 身份短语
   做等义替换。
3. **codebuff_metadata**：chat 请求体必须带 run_id / client_id / cost_mode:"free"
   / trace_session_id（同一对话跨轮复用，官方 sdk 从 previousRun 取）。

## 国别分层（官方 common/src/constants/freebuff-countries.ts，2026-09-12 实测）

  tier1 US；tier2 CA/GB/AU/NZ/IE/NO/SE/DK/FI/NL/AT/LU/IS；
  tier3 DE/FR/ES/IT/PT/BE/CH/LI/MT/KR —— 以上为 full access；
  **其余国家（含 CN/SG/JP）与任何 VPN/代理出口一律 limited access**（模型目录缩减，
  仍可用）。生产服务器在 CN、代理出口 SG/JP，故 AIGate 上这个渠道默认是 limited
  access —— 这是上游策略，不是接入 bug。

## 计费单位

Freebucks：**按次扣费的每日钱包**（不是「每模型每天 N 次」白名单）。
扣费发生在建 session 时；DELETE session 异步退款（freebucksRefundPending）。
两个 limit 并存，**只信 GET 快照的 daily.limit**（429 响应体的 limit 是当日闸值）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import secrets
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

FREEBUFF_API_BASE = "https://www.codebuff.com"
# 官方 SDK UA：free 模式识别依赖它（浏览器 UA 会被拒）
SDK_UA = "ai-sdk/openai-compatible/1.0.25/codebuff"
# 官方 free-mode marker：system 提示必须以它开头（字节级校验）
BUFFY = "You are Buffy, the strategic coding assistant."
# 官方 chat 的 stop 串（含引号的字面量）
CB_EASP_STOP = '"cb_easp"'
# 子 run agent（官方 context-pruner：对话超预算时压缩上下文）
CONTEXT_PRUNER_AGENT = "context-pruner"

HTTP_TIMEOUT = 30.0
SESSION_TIMEOUT = 15.0

# 模型 → 上游 root agentId（官方 free-agents.ts FREEBUFF_ROOT_AGENT_ID_BY_MODEL
# 2026-09-26 快照；未列出的走 base2-free 兜底，与官方 getFreebuffRootAgentId 一致）
MODEL_AGENT_MAP: dict = {
    "mimo/mimo-v2.5": "base2-free-mimo",
    "mimo/mimo-v2.6-pro": "base2-free-mimo-2-6-pro",
    "minimax/minimax-m3": "base2-free-minimax-m3",
    "openai/gpt-5.6-luna": "base2-free-luna",
    "openai/gpt-6-luna": "base2-free-luna-6",
    "upstage/solar-pro4": "base2-free-solar-pro4",
    "upstage/solar-mini4": "base2-free-solar-mini4",
    "deepseek/deepseek-v4-pro": "base2-free-deepseek",
    "deepseek/deepseek-v4-flash": "base2-free-deepseek-flash",
    "z-ai/glm-5.2": "base2-free-glm",
    "z-ai/glm-5.3-flash": "base2-free-glm-5-3-flash",
    "crof/kimi-k3-eco": "base2-free-kimi-k3-eco",
    "anthropic/claude-fable-5": "base2-free-fable",
    "meta/muse-spark-1.2-contributor": "base2-free-muse-spark",
    "google/gemini-3.8-flash": "base2-free-gemini-3-8-flash",
    "poolside/laguna-s-2.1": "base2-free-laguna-s-2-1",
    "openrouter/poolside/laguna-s-2.1": "base2-free-laguna-s-2-1-openrouter",
    "inclusionai/ling-3.0-flash:free": "base2-free-ling-3-flash",
    "crof/greg-2-ultra": "base2-free-greg-2-ultra",
    "crof/greg-2-super": "base2-free-greg-2-super",
}
DEFAULT_AGENT = "base2-free"

# 兜底模型目录（来自 freebuff2api-wokers releases/freebuff-models.json 2026-08-11 快照
# + README 2026-09-22 价格表补充）。在线目录不可用时用它。
FALLBACK_MODELS = [
    ("mimo/mimo-v2.5", "MiMo 2.5"),
    ("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash"),
    ("deepseek/deepseek-v4-flash-max", "DeepSeek V4 Flash Max"),
    ("deepseek/deepseek-v4-pro-max", "DeepSeek V4 Pro Max"),
    ("openai/gpt-5.6-luna", "GPT-5.6 Luna"),
    ("minimax/minimax-m3", "MiniMax M3"),
    ("z-ai/glm-5.2", "GLM 5.2"),
    ("z-ai/glm-5.3-flash", "GLM 5.3 Flash"),
    ("crof/kimi-k3-eco", "Kimi K3 Eco"),
    ("upstage/solar-pro4", "Solar Pro 4"),
    ("meta/muse-spark-1.2-contributor", "Muse Spark 1.2"),
    ("google/gemini-3.8-flash", "Gemini 3.8 Flash"),
]

# 官方外域 harness 身份短语 → 等义中性替换（照 worker.js HARNESS_PROMPT_REPLACEMENTS）
HARNESS_PROMPT_REPLACEMENTS = (
    ("You are Claude Code", "You are a coding assistant"),
    ("Anthropic's official CLI", "a command line interface"),
    ("cc_version=", "cli_version="),
)

MCP_TOOL_PREFIX = "mcp__"
SIGNATURE_TOOL_NAME = "decide"
SIGNATURE_TOOL = {
    "type": "function",
    "function": {
        "name": SIGNATURE_TOOL_NAME,
        "description": "Records the routing decision taken for the current step. Bookkeeping only.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "description": "Short label for the decision taken."},
            },
            "required": ["decision"],
        },
    },
}


# ── 纯函数（可单测，不碰网络）─────────────────────────────────

def agent_for_model(model: str) -> str:
    return MODEL_AGENT_MAP.get(model, DEFAULT_AGENT)


def to_wire_tool_name(name) -> str:
    """客户端工具名 → 上行工具名（总加一层 mcp__ 前缀，保证一一映射）。

    客户端自带的 mcp__xxx 会变成 mcp__mcp__xxx，还原后仍是 mcp__xxx。
    """
    if not isinstance(name, str) or name == "":
        return name
    return MCP_TOOL_PREFIX + name


def from_wire_tool_name(name):
    """上行工具名 → 客户端原始名（响应侧还原）。"""
    if not isinstance(name, str):
        return name
    return name[len(MCP_TOOL_PREFIX):] if name.startswith(MCP_TOOL_PREFIX) else name


def strip_harness_markers(text: str) -> str:
    """把 system 里的外域 harness 身份短语替换成语义等义的中性文本。

    官方按子串包含判定（FOREIGN_HARNESS_SYSTEM_PROMPTS），命中即静默降级。
    用等义替换而不是删除，避免留下 ", for Claude (2.0.1)" 这类破碎语句。
    """
    if not isinstance(text, str) or text == "":
        return text
    out = text
    for marker, neutral in HARNESS_PROMPT_REPLACEMENTS:
        if marker in out:
            out = out.replace(marker, neutral)
    return out


def normalize_messages(messages: list) -> list:
    """system 消息：剥 harness 身份短语 + 注入 Buffy 前缀（官方字节级校验）。"""
    if not isinstance(messages, list):
        return []
    out = []
    has_system = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        item = dict(m)
        if item.get("role") == "developer":
            item["role"] = "system"
        if item.get("role") == "system":
            has_system = True
            content = item.get("content")
            if isinstance(content, str):
                item["content"] = _buffy(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        part["text"] = _buffy(part["text"])
        out.append(item)
    if not has_system:
        out.insert(0, {"role": "system", "content": BUFFY})
    return out


def _buffy(text: str) -> str:
    stripped = strip_harness_markers(text)
    if stripped.startswith(BUFFY):
        return stripped
    return BUFFY + stripped


def trace_session_id(token: str) -> str:
    """会话级 trace id：官方同一对话跨轮复用、新对话才 randomUUID。

    代理无状态 → 按「token × 30 分钟滚动窗口」派生恒定 UUID v4 形状，
    跨窗口轮换 ≈ 新对话（照 worker.js）。
    """
    window = int(time.time() // (30 * 60))
    h1 = _fnv(f"freebuff-fp-v2:{token}|ts1|{window}")
    h2 = _fnv(f"freebuff-fp-v2:{token}|ts2|{window}")
    hexs = (h1 + h2)[:32].ljust(32, "0")
    variant = "89ab"[int(hexs[16], 16) % 4]
    return f"{hexs[:8]}-{hexs[8:12]}-4{hexs[13:16]}-{variant}{hexs[17:20]}-{hexs[20:32]}"


def _fnv(s: str) -> str:
    h = 0x811C9DC5
    for ch in s:
        h = (h ^ ord(ch)) * 0x01000193 & 0xFFFFFFFF
    return f"{h:08x}"


def prompt_id() -> str:
    """client_id = 官方 promptId（Math.random().toString(36).substring(2,15) 等价）。"""
    return "".join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(13))


def apply_tool_disguise(payload: dict) -> dict:
    """工具集出站伪装（照 worker.js buildUpstreamPayload 的工具段）。

    客户端工具统一挂 mcp__ 前缀 + 注入官方 decide 签名工具 + tool_choice 同步改名。
    没有 tools 时不动（官方只对带 tools 的请求做外域检测）。
    """
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return payload
    new_tools = []
    for t in tools:
        if isinstance(t, dict) and isinstance(t.get("function"), dict) \
                and isinstance(t["function"].get("name"), str):
            fn = dict(t["function"])
            fn["name"] = to_wire_tool_name(fn["name"])
            new_tools.append({**t, "function": fn})
        else:
            new_tools.append(t)
    if not any(isinstance(t, dict) and isinstance(t.get("function"), dict)
               and t["function"].get("name") == SIGNATURE_TOOL_NAME for t in new_tools):
        new_tools.append(SIGNATURE_TOOL)
    payload["tools"] = new_tools
    tc = payload.get("tool_choice")
    if isinstance(tc, dict) and isinstance(tc.get("function"), dict) \
            and isinstance(tc["function"].get("name"), str):
        payload["tool_choice"] = {**tc, "function": {**tc["function"],
                                                     "name": to_wire_tool_name(tc["function"]["name"])}}
    return payload


# 客户端请求 → 上游 payload 可透传的键（照 worker.js UPSTREAM_KEYS）
UPSTREAM_KEYS = (
    "frequency_penalty", "logit_bias", "logprobs", "max_completion_tokens", "max_tokens",
    "metadata", "modalities", "parallel_tool_calls", "presence_penalty", "reasoning_effort",
    "response_format", "seed", "service_tier", "stop", "store", "stream_options",
    "temperature", "tool_choice", "tools", "top_logprobs", "top_p", "top_k", "user",
)


def build_chat_payload(body: dict, model: str, run_id: str, instance_id: str,
                       token: str) -> dict:
    """构造上游 chat 请求体（照 worker.js buildUpstreamPayload，2026-09 协议对齐）。"""
    payload = {k: body[k] for k in UPSTREAM_KEYS if body.get(k) is not None}
    payload["model"] = model
    payload["messages"] = normalize_messages(body.get("messages") or [])
    # 上游强制流式：无论客户端要什么，出站恒为 stream=true（非流式由网关聚合）
    payload["stream"] = True
    # 官方 SDK 恒发 include_usage（model-provider.ts includeUsage:true）
    payload["stream_options"] = {"include_usage": True}
    if not payload.get("stop"):
        payload["stop"] = [CB_EASP_STOP]
    payload["provider"] = {"data_collection": "deny"}
    apply_tool_disguise(payload)
    payload["codebuff_metadata"] = {
        "freebuff_instance_id": instance_id,
        "trace_session_id": trace_session_id(token),
        "run_id": run_id,
        "client_id": prompt_id(),
        "cost_mode": "free",
    }
    return payload


def restore_tool_names(obj: dict) -> dict:
    """响应侧把上行工具名还原成客户端原始名（剥 mcp__ 前缀）。"""
    if not isinstance(obj, dict):
        return obj
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        return obj
    choice = choices[0]
    if not isinstance(choice, dict):
        return obj
    for holder_key in ("delta", "message"):
        holder = choice.get(holder_key)
        if not isinstance(holder, dict):
            continue
        tcs = holder.get("tool_calls")
        if not isinstance(tcs, list):
            continue
        for tc in tcs:
            if isinstance(tc, dict) and isinstance(tc.get("function"), dict) \
                    and isinstance(tc["function"].get("name"), str):
                tc["function"]["name"] = from_wire_tool_name(tc["function"]["name"])
    return obj


def parse_sse_data(line: str) -> Optional[dict]:
    """上游 SSE 行 → dict。

    上游可能把 OpenAI chunk 包在 {"data": {...}} 信封里（worker.js 实测），
    这里统一剥壳；非 data 行返回 None。
    """
    if not line.startswith("data:"):
        return None
    raw = line[5:].strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if isinstance(obj, dict) and isinstance(obj.get("data"), dict) \
            and (obj["data"].get("choices") or obj["data"].get("id") or obj["data"].get("usage")):
        return obj["data"]
    return obj


# ── 网络：登录（CLI 授权码轮询，与官方 CLI 同协议）──────────────

def gen_fingerprint() -> str:
    """官方 legacy fallback 指纹格式：codebuff-cli-<8 位随机>。"""
    return "codebuff-cli-" + secrets.token_urlsafe(6)[:8]


async def start_cli_login(proxy: Optional[str] = None) -> Tuple[Optional[dict], str]:
    """POST /api/auth/cli/code → {loginUrl, fingerprintHash, expiresAt, fingerprintId}。

    返回 (info, err)。用户在浏览器打开 loginUrl 用 Google 登录后，
    由调用方轮询 poll_cli_login 收 authToken。
    """
    fingerprint_id = gen_fingerprint()
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, proxy=proxy) as c:
            r = await c.post(f"{FREEBUFF_API_BASE}/api/auth/cli/code",
                             json={"fingerprintId": fingerprint_id},
                             headers={"Content-Type": "application/json",
                                      "Accept": "application/json", "User-Agent": SDK_UA})
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        data = r.json()
    except ValueError:
        return None, "登录响应不是 JSON"
    if not data.get("loginUrl") or not data.get("fingerprintHash"):
        return None, f"登录响应缺字段：{str(data)[:200]}"
    return {
        "fingerprint_id": fingerprint_id,
        "login_url": data["loginUrl"],
        "fingerprint_hash": data["fingerprintHash"],
        "expires_at": data.get("expiresAt"),
        "expires_in_ms": data.get("expiresInMs") or 3600_000,
    }, ""


async def poll_cli_login(info: dict, proxy: Optional[str] = None) -> Tuple[Optional[dict], str, bool]:
    """GET /api/auth/cli/status → (user, err, pending)。

    401 = 用户还没完成登录（pending）；400 = 授权请求已失效（终态）；
    200 + user.authToken = 成功。
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, proxy=proxy) as c:
            r = await c.get(f"{FREEBUFF_API_BASE}/api/auth/cli/status",
                            params={"fingerprintId": info.get("fingerprint_id"),
                                    "fingerprintHash": info.get("fingerprint_hash"),
                                    "expiresAt": info.get("expires_at")},
                            headers={"Accept": "application/json", "User-Agent": SDK_UA})
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}", True   # 网络抖动按 pending 处理
    if r.status_code == 401:
        return None, "等待登录", True
    if r.status_code == 400:
        return None, f"授权请求已失效：{r.text[:150]}", False
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}: {r.text[:150]}", True
    try:
        data = r.json()
    except ValueError:
        return None, "状态响应不是 JSON", True
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict) or not user.get("authToken"):
        return None, "等待登录", True
    return user, "", False


# ── 网络：session / run / chat ───────────────────────────────

class SessionError(Exception):
    """session 生命周期错误（带上游状态语义，供上层决定换号/终止）。"""

    def __init__(self, message: str, status: int = 0, terminal: bool = False):
        super().__init__(message)
        self.status = status
        self.terminal = terminal


class ModelUnavailable(Exception):
    """410 model_unavailable：官方已下线该模型，全局失败（不换号、不换模型）。"""


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "User-Agent": SDK_UA}


async def query_snapshot(token: str, proxy: Optional[str] = None) -> Tuple[Optional[dict], str]:
    """GET /api/v1/freebuff/session（只读额度快照，0 消耗、不创建 session）。

    `x-freebuff-include-unused-rate-limits: 1` 让响应直接回吐 freebucks
    （额度/价格/off-peak/重置时间），不用等 429 才知道还剩多少。
    """
    try:
        async with httpx.AsyncClient(timeout=SESSION_TIMEOUT, proxy=proxy) as c:
            r = await c.get(f"{FREEBUFF_API_BASE}/api/v1/freebuff/session",
                            headers={**_auth_headers(token),
                                     "x-freebuff-include-unused-rate-limits": "1"})
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"
    try:
        data = r.json()
    except ValueError:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    return {"status_code": r.status_code, "data": data}, ""


async def create_session(token: str, model: str, instance_id: Optional[str] = None,
                         proxy: Optional[str] = None) -> dict:
    """POST /api/v1/freebuff/session（预生成 instance-id，照 worker.js 单会话签名）。

    返回 {instance_id, model, expires_at, raw}；抛 SessionError / ModelUnavailable。

    409 冲突处理（照 worker.js）：单账号同一时刻只能一个 session，旧 session 若还
    占着锁，POST 会 409。**只重试一次**「GET 拿持锁 instanceId → DELETE 释放 → 再 POST」
    （DELETE 上游会异步退 Freebucks）。旧逻辑直接抛错 → 切模型会一直失败，且旧
    session 继续占锁。实测触发：用户上一次请求的 session 未释放就换模型。
    """
    model_unlock_retried = False
    for attempt in range(2):
        inst = instance_id or _uuid4()
        try:
            async with httpx.AsyncClient(timeout=SESSION_TIMEOUT, proxy=proxy) as c:
                r = await c.post(f"{FREEBUFF_API_BASE}/api/v1/freebuff/session",
                                 headers={**_auth_headers(token),
                                          "x-freebuff-model": model,
                                          "x-freebuff-instance-id": inst,
                                          "Content-Type": "application/json"},
                                 content=b"{}")
        except Exception as e:
            raise SessionError(f"{type(e).__name__}: {str(e)[:150]}", 0)
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"_text": r.text[:200]}
        if r.status_code == 410 or _has_exact_code(data, "model_unavailable"):
            raise ModelUnavailable(str(data.get("message") or data.get("error") or "model_unavailable")[:200])
        if r.status_code == 200 and data.get("status") == "active" and data.get("instanceId"):
            return {"instance_id": data["instanceId"], "model": data.get("model") or model,
                    "expires_at": data.get("expiresAt") or data.get("expires_at"), "raw": data}
        if r.status_code == 200 and data.get("status") == "queued":
            raise SessionError(f"session 排队中（{data.get('instanceId') or 'no-instance'}）", r.status_code)
        if r.status_code == 409:
            # ⚠️ 档位拒绝时错误码在 `error`、原因在 `message`：必须拿**整包**判定，
            # 只看 message 会漏判（上游 message 里没有 "session_model_mismatch" 字样）。
            blob = json.dumps(data, ensure_ascii=False)
            code = str(data.get("error") or data.get("code") or "")
            msg = str(data.get("message") or data.get("error") or "模型锁冲突")
            # 档位拒绝与「session 脏」是两回事：前者重试同一个模型必然再失败，
            # 且占着单会话锁 —— 交给调用方释放，并给出可执行提示。
            if is_model_not_entitled(blob):
                raise SessionError(model_gate_message(model, blob), r.status_code)
            # 其它 409 = 旧 session 还占着单会话锁 → 释放后重试一次
            if not model_unlock_retried:
                model_unlock_retried = True
                holder = await current_session_instance(token, proxy=proxy)
                if holder:
                    await delete_session(token, holder, proxy=proxy)
                    logger.info("freebuff: 释放占锁 session %s 后重试（%s）", holder[:8], msg[:80])
                    continue
            # 其余 409 必须**如实回显上游错误码**，不能一律盖成 session_model_mismatch：
            # 上游对多种情况都回 409（实测 session_superseded / model_locked 等），
            # 盖错码会把「另一个实例占了 session」误导成「模型不在此档」。
            raise SessionError(f"{code or 'session_conflict'}: {msg[:150]}", r.status_code)
        if r.status_code == 403:
            st = data.get("status")
            if st == "banned":
                raise SessionError("账号已被封禁（Terminal，不可恢复）", r.status_code, terminal=True)
            if st == "country_blocked":
                raise SessionError(f"地区受限（{data.get('countryCode') or 'UNKNOWN'}）", r.status_code, terminal=True)
        if r.status_code == 401:
            raise SessionError("authToken 无效或已撤销（请重新连接）", r.status_code, terminal=True)
        if r.status_code == 429:
            raise SessionError(f"Freebucks 额度已用尽：{str(data.get('message') or '')[:120]}", r.status_code)
        raise SessionError(f"create session failed: HTTP {r.status_code} {str(data)[:150]}", r.status_code)
    raise SessionError("create session failed: 释放占锁 session 后仍冲突", 409)


async def current_session_instance(token: str, proxy: Optional[str] = None) -> Optional[str]:
    """GET /api/v1/freebuff/session → 当前活跃 session 的 instanceId（无则 None）。

    ⚠️ 只在**需要释放占锁 session** 时调用（worker.js 同款用法）：这个 GET 本身
    会占用账号 session，为查额度而随手调用会顶掉正在进行的 chat（worker.js 明确
    警告过两次）。查额度请用 query_snapshot()。
    """
    try:
        async with httpx.AsyncClient(timeout=SESSION_TIMEOUT, proxy=proxy) as c:
            r = await c.get(f"{FREEBUFF_API_BASE}/api/v1/freebuff/session",
                            headers=_auth_headers(token))
        data = r.json() if r.content else {}
    except Exception:
        return None
    if isinstance(data, dict) and data.get("status") == "active":
        inst = data.get("instanceId")
        return str(inst) if inst else None
    return None


async def delete_session(token: str, instance_id: str, proxy: Optional[str] = None) -> bool:
    """DELETE /api/v1/freebuff/session（上游异步退 Freebucks）。"""
    if not instance_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=SESSION_TIMEOUT, proxy=proxy) as c:
            r = await c.request("DELETE", f"{FREEBUFF_API_BASE}/api/v1/freebuff/session",
                                headers={**_auth_headers(token),
                                         "x-freebuff-instance-id": instance_id},
                                json={"instanceId": instance_id})
        return r.status_code < 400
    except Exception:
        return False


async def start_run(token: str, agent_id: str, ancestors: Optional[list] = None,
                    proxy: Optional[str] = None) -> str:
    """POST /api/v1/agent-runs {action:START} → runId。"""
    try:
        async with httpx.AsyncClient(timeout=SESSION_TIMEOUT, proxy=proxy) as c:
            r = await c.post(f"{FREEBUFF_API_BASE}/api/v1/agent-runs",
                             headers={**_auth_headers(token), "Content-Type": "application/json"},
                             json={"action": "START", "agentId": agent_id,
                                   "ancestorRunIds": ancestors or []})
    except Exception as e:
        raise SessionError(f"start_run 网络错误：{type(e).__name__}", 0)
    if r.status_code >= 400:
        raise SessionError(f"start_run failed: HTTP {r.status_code} {r.text[:150]}", r.status_code)
    data = r.json() if r.content else {}
    run_id = data.get("runId")
    if not run_id:
        raise SessionError(f"start_run 无 runId：{str(data)[:150]}", r.status_code)
    return run_id


async def finish_run(token: str, run_id: str, proxy: Optional[str] = None) -> None:
    """POST /api/v1/agent-runs {action:FINISH}（尽力而为，失败不影响响应）。"""
    if not run_id:
        return
    try:
        async with httpx.AsyncClient(timeout=SESSION_TIMEOUT, proxy=proxy) as c:
            await c.post(f"{FREEBUFF_API_BASE}/api/v1/agent-runs",
                         headers={**_auth_headers(token), "Content-Type": "application/json"},
                         json={"action": "FINISH", "runId": run_id, "status": "completed",
                               "totalSteps": 1, "directCredits": 0, "totalCredits": 0})
    except Exception:
        pass


def _has_exact_code(data, code: str) -> bool:
    """错误码精确匹配（上游可能把 code 放在 error.code / code / error 里）。"""
    if not isinstance(data, dict):
        return False
    for key in ("code", "error", "type"):
        v = data.get(key)
        if isinstance(v, str) and v == code:
            return True
        if isinstance(v, dict) and v.get("code") == code:
            return True
    return False


# ── 档位闸门（免费层按出口/账号档位放行模型）───────────────────
# `session_model_mismatch` 的实际语义是「这个模型不在你的档位里」，不是 session 脏。
# 上游文案只列它自己认的短名（GLM 5.3 Flash / DeepSeek V4.1 Flash …），与 AIGate
# 目录里的 id 对不上，用户照着重试仍会失败 —— 故统一转成可执行提示。
# 实测（2026-09-28，CN 出口 limited 档）：GET 快照的 rateLimitsByModel 键集合
# 与放行清单一一对应（deepseek/deepseek-v4-flash、mimo/mimo-v2.5、
# upstage/solar-mini4、upstage/solar-pro4、z-ai/glm-5.3-flash）。
NOT_ENTITLED_MARKERS = ("session_model_mismatch", "not valid for limited access")


def is_model_not_entitled(text: str) -> bool:
    """错误文本是否表示「模型不在此档」（而非 session 脏/失效）。"""
    d = (text or "").lower()
    return any(m in d for m in NOT_ENTITLED_MARKERS)


def model_gate_message(model: str, detail: str = "") -> str:
    """档位拒绝 → 可执行提示（列出实测放行模型，不编造）。"""
    upstream = ""
    try:
        obj = json.loads(detail)
        if isinstance(obj, dict):
            upstream = str(obj.get("message") or obj.get("error") or "")
    except (ValueError, TypeError):
        upstream = detail or ""
    tail = f"；上游原文：{upstream[:200]}" if upstream else ""
    return (f"freebuff 免费层不提供该模型（{model}）：当前出口档位只放行部分模型。"
            f"请改用可用模型（DeepSeek V4 Flash / MiMo 2.5 / Solar Mini 4 / "
            f"Solar Pro 4 / GLM 5.3 Flash），或在服务商设置里开启「走代理」"
            f"换到 full access 出口{tail}")


def entitled_model_ids(snapshot: dict) -> list:
    """从 GET /session 快照取当前档位放行的模型 id（无信息时返回空表）。

    只读、0 消耗；拿不到就返回空 —— 调用方按「未知」处理，绝不据此删模型。
    """
    if not isinstance(snapshot, dict):
        return []
    rlm = snapshot.get("rateLimitsByModel")
    if not isinstance(rlm, dict):
        return []
    return [str(k) for k in rlm.keys() if isinstance(k, str) and k]


# ── 档位放行清单缓存（仅供显示名标注，绝不用于过滤）───────────────
# 只在**已经有**快照的地方顺手记下（额度查询 / 健康探测），不额外发请求 ——
# worker.js 明确警告过：为查状态而 GET /session 会顶掉正在进行的 chat。
# 拿不到就当作「未知」，模型照常列出、照常可点名调用，失败由上游如实报错。
_ENTITLED_CACHE: dict = {}          # token_hash -> (expires_monotonic, frozenset)
ENTITLED_CACHE_TTL = 300.0


def _token_fp(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()[:20]


def note_entitlement(token: str, snapshot: dict) -> None:
    """把快照里的放行清单记进缓存（无信息则不动，避免用空表覆盖已知值）。"""
    ids = entitled_model_ids(snapshot)
    if not ids:
        return
    _ENTITLED_CACHE[_token_fp(token)] = (time.monotonic() + ENTITLED_CACHE_TTL, frozenset(ids))


def known_entitlement(token: str):
    """已知的放行模型集合；未知（缓存过期/从未探测）返回 None。

    返回 None 与返回空集语义不同：None = 不知道（不标注），空集 = 上游明确说一个都不放行。
    """
    hit = _ENTITLED_CACHE.get(_token_fp(token))
    if not hit or hit[0] <= time.monotonic():
        return None
    return hit[1]


def _uuid4() -> str:
    import uuid
    return str(uuid.uuid4())


# ── 模型目录 ────────────────────────────────────────────────

RELEASE_MODELS_URL = ("https://github.com/pingmike2/freebuff2api-wokers"
                      "/releases/latest/download/freebuff-models.json")


async def fetch_release_models(proxy: Optional[str] = None) -> list:
    """从社区仓库的 releases 资产拉预解析的模型清单（worker.js 同款兜底源）。

    拉不到就返回空列表，由调用方回退 FALLBACK_MODELS（绝不因此让刷新失败）。
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, proxy=proxy, follow_redirects=True) as c:
            r = await c.get(RELEASE_MODELS_URL, headers={"User-Agent": SDK_UA})
        if r.status_code >= 400:
            return []
        data = r.json()
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        out = []
        for m in models:
            if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]:
                out.append(m["id"])
        return out
    except Exception:
        return []
