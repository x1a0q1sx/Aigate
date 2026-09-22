"""OpenCode Free 的 CLI sidecar 桥接。

2026-09-22 取证结论：opencode.ai 免费层把入口锁在「官方 CLI 进程建立的会话」里
（复刻请求头/body、bun 运行时、curl h2、9router 头部形态全部 403；官方 CLI 直连 200；
抓包得到的 session 值复用即 200、全新 session 一律 403）。9router 的真实做法是让
**本机官方 CLI 当上游客户端**、自己只做透传——本模块即复刻该架构：

    AIGate 请求 ──> 本模块 ──(HTTP)──> opencode serve（常驻官方 CLI）
                                        └──(真 CLI 会话)──> opencode.ai ✅

部署：CLI 二进制放 AIGATE_OPENCODE_BIN（默认 ~/opencode/bin/opencode），常驻 `opencode serve`
（127.0.0.1:4096，仅本机可达）。本模块只做「建 session → 发 prompt → 收回复」，
不依赖 CLI 的内部实现细节（走它公开的 HTTP API，见 /doc OpenAPI）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import AsyncGenerator, Optional

import httpx

logger = logging.getLogger(__name__)

# sidecar 基址（可用环境变量覆盖，便于测试与多实例）
SIDECAR_BASE = os.environ.get("AIGATE_OPENCODE_SIDECAR", "http://127.0.0.1:4096")
_SIDECAR_TIMEOUT = float(os.environ.get("AIGATE_OPENCODE_TIMEOUT", "180"))
# 网关专用 agent：CLI 默认 agent 带「编码助手」人格（实测会回 "Hi! I'm ready to help with
# your workspace at ..."），与 chat completions 的直出语义不符。用 CLI 的 agent 配置能力
# 定义 aigate 纯对话 agent（提示词约束直接作答、不寒暄/不自述/不追问），建 session 时指定。
# 未配置该 agent 时自动回退 CLI 默认（不影响可用性，只是回复带人格）。
SIDECAR_AGENT = os.environ.get("AIGATE_OPENCODE_AGENT", "aigate")


class SidecarUnavailable(RuntimeError):
    """sidecar 不可用（未启动/端口不通）——调用方应回退其它候选而非判死该模型。"""


async def sidecar_alive(client: Optional[httpx.AsyncClient] = None) -> bool:
    """探测 sidecar 是否在线（/api/health 或根路径）。"""
    own = client is None
    c = client or httpx.AsyncClient(timeout=5.0)
    try:
        for path in ("/api/health", "/global/health", "/api/model"):
            try:
                r = await c.get(SIDECAR_BASE + path)
                if r.status_code < 500:
                    return True
            except Exception:
                continue
        return False
    finally:
        if own:
            await c.aclose()


async def _create_session(client: httpx.AsyncClient, provider_id: str, model_id: str,
                          title: str = "") -> str:
    # 唯一标题：便于排查「回复串到别的会话」这类问题（实测 CLI 偶发把历史会话内容带出）
    if not title:
        title = f"aigate-{os.urandom(4).hex()}"
    payload = {
        "title": title,
        "model": {"id": model_id, "providerID": provider_id},
    }
    if SIDECAR_AGENT:
        payload["agent"] = SIDECAR_AGENT
    r = await client.post(f"{SIDECAR_BASE}/api/session", json=payload)
    if r.status_code >= 400 and SIDECAR_AGENT:
        # agent 未配置/不被接受时回退默认 agent，不让整个请求失败
        payload.pop("agent", None)
        r = await client.post(f"{SIDECAR_BASE}/api/session", json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"opencode sidecar 建 session 失败 HTTP {r.status_code}: {r.text[:200]}")
    data = (r.json() or {}).get("data") or {}
    sid = data.get("id")
    if not sid:
        raise RuntimeError(f"opencode sidecar 未返回 session id: {r.text[:200]}")
    return sid


async def _post_prompt(client: httpx.AsyncClient, sid: str, text: str,
                       files: Optional[list] = None) -> str:
    """发 prompt，返回 sidecar 分配的用户消息 id（`msg_...`）——用于把回复精确关联到本次提问。"""
    payload = {"prompt": {"text": text}}
    if files:
        payload["prompt"]["files"] = files
    r = await client.post(f"{SIDECAR_BASE}/api/session/{sid}/prompt", json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"opencode sidecar 发 prompt 失败 HTTP {r.status_code}: {r.text[:200]}")
    data = (r.json() or {}).get("data") or {}
    return str(data.get("id") or "")


async def _user_msg_created_at(client: httpx.AsyncClient, sid: str,
                               user_msg_id: str) -> Optional[int]:
    """取本次用户消息的 created 时间戳（作为「本轮回复」的归属基线）。"""
    if not user_msg_id:
        return None
    try:
        r = await client.get(f"{SIDECAR_BASE}/api/session/{sid}/message")
        if r.status_code >= 400:
            return None
        for it in (r.json() or {}).get("data") or []:
            if isinstance(it, dict) and it.get("id") == user_msg_id:
                return int(((it.get("time") or {}).get("created") or 0)) or None
    except Exception:
        return None
    return None


def _content_to_text(content) -> tuple[str, str]:
    """把 assistant 的 content 数组拆成 (正文, 思考内容)。

    CLI 的 assistant 消息形态（实测）：
      {"type":"assistant","content":[{"type":"reasoning","text":...},
                                     {"type":"text","text":...}], "finish":"stop",
       "tokens":{"input":..,"output":..,"reasoning":..}}
    """
    text_parts, reason_parts = [], []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "text":
            text_parts.append(str(p.get("text") or ""))
        elif t in ("reasoning", "thinking"):
            reason_parts.append(str(p.get("text") or ""))
    return "".join(text_parts), "".join(reason_parts)


async def _await_assistant(client: httpx.AsyncClient, sid: str, *, since_ms: int,
                           poll_interval: float = 0.6, deadline_s: float = None):
    """轮询消息列表直到出现「本轮的 assistant 消息且已生成完毕」；返回 (消息 dict, 错误 dict|None)。

    两个实测坑：
    1) assistant 消息**逐步填充**——先出现 time.created 与空的 reasoning part，随后才补
       text 并写入 time.completed。若只判「消息存在」会拿到空回复。
    2) 偶发把**历史会话内容**带出（首轮实测见过返回无关的 "## Intuition ... linked list"）。
       故这里对 assistant 做双重过滤：① created 必须 ≥ 本次提问时间；② 必须已 completed/finish。
       只有同时满足才认。
    """
    deadline = time.monotonic() + (deadline_s if deadline_s is not None else _SIDECAR_TIMEOUT)
    latest = None
    while time.monotonic() < deadline:
        try:
            r = await client.get(f"{SIDECAR_BASE}/api/session/{sid}/message")
            if r.status_code < 400:
                items = (r.json() or {}).get("data") or []
                for it in items:
                    if not isinstance(it, dict) or it.get("type") != "assistant":
                        continue
                    created = ((it.get("time") or {}).get("created") or 0)
                    if created and created < since_ms:
                        continue        # 属于更早的对话，不认
                    tm = it.get("time") or {}
                    done = bool(tm.get("completed") or it.get("finish"))
                    if not done:
                        continue        # 仍在流式填充中，等下一轮
                    if it.get("error"):
                        return it, it.get("error")
                    return it, None     # 本轮且已完成
        except Exception as e:      # 轮询期瞬时错误不致命
            logger.debug("opencode sidecar poll: %s", e)
        await asyncio.sleep(poll_interval)
    if latest is not None:
        # 超时但已有部分内容：返回已拿到的（调用方按 finish 判定完整性）
        return latest, latest.get("error")
    raise TimeoutError(f"opencode sidecar 等待回复超时（{_SIDECAR_TIMEOUT}s）")


def _to_openai_response(msg: dict, model_id: str, prompt_text: str) -> dict:
    """sidecar 的 assistant 消息 → OpenAI 兼容响应（供 AIGate 内部统一处理）。"""
    text, reasoning = _content_to_text(msg.get("content"))
    tk = msg.get("tokens") or {}
    created = int(((msg.get("time") or {}).get("created") or time.time() * 1000) / 1000)
    message = {"role": "assistant", "content": text}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": msg.get("id") or f"chatcmpl-{int(time.time()*1000)}",
        "object": "chat.completion",
        "created": created,
        "model": model_id,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": msg.get("finish") or "stop",
        }],
        "usage": {
            "prompt_tokens": int(tk.get("input") or 0),
            "completion_tokens": int(tk.get("output") or 0),
            "total_tokens": int(tk.get("input") or 0) + int(tk.get("output") or 0),
        },
        # 供日志/调试：sidecar 侧的会话 id（非上游协议字段，调用方不应外发）
        "_sidecar_session": msg.get("sessionID"),
    }


async def chat_completion(messages: list, model_id: str, provider_id: str = "opencode",
                          files: Optional[list] = None, client: Optional[httpx.AsyncClient] = None) -> dict:
    """非流式：把 OpenAI 风格 messages 交给 sidecar（官方 CLI）执行，返回 OpenAI 兼容响应。

    messages 会被压平成一段文本（CLI 的 prompt API 只收 text + files）——
    这是与官方 CLI 的固有差异：它的 API 面向「人输一句话」，不是多轮 message 数组。
    对网关用途（combo 里当免费候选）足够；system 消息以标签包裹附在前部以保留语义。
    """
    prompt_text = flatten_messages(messages)
    own = client is None
    c = client or httpx.AsyncClient(timeout=_SIDECAR_TIMEOUT)
    try:
        sid = await _create_session(c, provider_id, model_id)
        t0 = int(time.time() * 1000)
        user_msg_id = await _post_prompt(c, sid, prompt_text, files=files)
        # 用 sidecar 分配的用户消息 id 的时间戳做归属基线（比本地 t0 更贴近服务端时钟）
        since = await _user_msg_created_at(c, sid, user_msg_id) or t0
        msg, err = await _await_assistant(c, sid, since_ms=since)
        if err:
            raise RuntimeError(f"opencode sidecar 上游报错: {json.dumps(err, ensure_ascii=False)[:300]}")
        resp = _to_openai_response(msg, model_id, prompt_text)
        await _close_session(c, sid)
        return resp
    finally:
        if own:
            await c.aclose()


async def _close_session(client: httpx.AsyncClient, sid: str) -> None:
    """尽力关闭 session（避免 sidecar 里会话堆积）；失败不影响结果。"""
    try:
        await client.delete(f"{SIDECAR_BASE}/api/session/{sid}")
    except Exception:
        pass


def flatten_messages(messages: list) -> str:
    """OpenAI messages → 单段文本（保留角色语义，供 CLI prompt 使用）。

    行为语义由 sidecar 的 `aigate` agent（SIDECAR_AGENT）负责——那里定义了
    「直接作答、不寒暄/不自述/不追问」的系统提示词。这里只做消息拼接，
    不再叠加前置指令（避免与 agent 提示词重复、互相干扰）。
    """
    return _flatten_body(messages)


def _flatten_body(messages: list) -> str:
    chunks = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, list):
            # typed blocks → 拼接文本
            text = "".join(
                str(p.get("text") or "") for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            )
        else:
            text = str(content or "")
        if not text:
            continue
        if role == "system":
            chunks.append(f"[system]\n{text}")
        elif role == "assistant":
            chunks.append(f"[assistant]\n{text}")
        elif role == "tool":
            chunks.append(f"[tool]\n{text}")
        else:
            chunks.append(text)
    return "\n\n".join(chunks)


async def stream_chat_completion(messages: list, model_id: str, provider_id: str = "opencode",
                                 client: Optional[httpx.AsyncClient] = None) -> AsyncGenerator[dict, None]:
    """流式：sidecar 的 message API 非流式，这里做「一次拿全 + 切块吐出」。

    保持与 openai_compat adapter 流式相同的 chunk 形态，让 combo/auto 的
    「实质内容锁定」「race」「drool guard」等既有逻辑无需改动即可工作。
    """
    resp = await chat_completion(messages, model_id, provider_id=provider_id, client=client)
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    rid = resp.get("id") or f"chatcmpl-{int(time.time()*1000)}"
    base = {"id": rid, "object": "chat.completion.chunk", "created": resp.get("created") or int(time.time()),
            "model": resp.get("model") or model_id}

    def chunk(delta, finish=None):
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant", "content": ""})
    if reasoning:
        yield chunk({"reasoning_content": reasoning})
    if text:
        # 切块（保持可观测的流式节奏；步长按 24 字符）
        for i in range(0, len(text), 24):
            yield chunk({"content": text[i:i + 24]})
    usage = resp.get("usage") or {}
    yield chunk({}, choice.get("finish_reason") or "stop")
    if usage:
        yield {**base, "choices": [], "usage": usage}
