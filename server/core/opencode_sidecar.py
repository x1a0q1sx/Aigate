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

# ─────────────────────────── 配置解析：config.yaml > 环境变量 > 内置默认 ───────────────────────────
# config.yaml 的 opencode_bridge 段可热改（设置页保存即生效），环境变量作为兼容旧部署的兜底。

def _cfg():
    """取配置段；未初始化 / 旧配置无该段时回退内置默认（绝不抛异常）。"""
    try:
        from server.config import get_config
        c = getattr(get_config(), "opencode_bridge", None)
        if c is not None:
            return c
    except Exception:
        pass
    return None


def _setting(name: str, env: str, default):
    """读取一项配置：config.yaml 优先，其次环境变量，最后内置默认。"""
    c = _cfg()
    if c is not None:
        v = getattr(c, name, None)
        if v is not None and v != "":
            return v
    v = os.environ.get(env)
    if v is not None and v != "":
        return v
    return default


def sidecar_base() -> str:
    return str(_setting("base_url", "AIGATE_OPENCODE_SIDECAR", "http://127.0.0.1:4096"))


def bridge_enabled() -> bool:
    """桥接总开关（config.yaml 的 opencode_bridge.enabled）。"""
    return bool(_setting("enabled", "AIGATE_OPENCODE_ENABLED", True))


def sidecar_timeout() -> float:
    return float(_setting("timeout_seconds", "AIGATE_OPENCODE_TIMEOUT", 180))


def sidecar_agent() -> str:
    return str(_setting("agent", "AIGATE_OPENCODE_AGENT", "aigate"))


def sidecar_poll_interval() -> float:
    return max(0.05, float(_setting("poll_interval_ms", "AIGATE_OPENCODE_POLL_MS", 600)) / 1000.0)


def stall_grace_seconds() -> float:
    """本轮多久没有新进展即判停滞（随后 interrupt 收尾）。"""
    try:
        return max(5.0, float(_setting("stall_grace_seconds", "AIGATE_OPENCODE_STALL", 20)))
    except Exception:
        return 20.0


def auto_reject_tools() -> bool:
    return bool(_setting("auto_reject_tools", "AIGATE_OPENCODE_AUTO_REJECT", True))


# 兼容旧引用（模块级常量语义已变为「启动时快照」，运行期请用上面的函数）
SIDECAR_BASE = os.environ.get("AIGATE_OPENCODE_SIDECAR", "http://127.0.0.1:4096")
_SIDECAR_TIMEOUT = float(os.environ.get("AIGATE_OPENCODE_TIMEOUT", "180"))
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
                r = await c.get(sidecar_base() + path)
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
    agent = sidecar_agent()
    if agent:
        payload["agent"] = agent
    r = await client.post(f"{sidecar_base()}/api/session", json=payload)
    if r.status_code >= 400 and agent:
        # agent 未配置/不被接受时回退默认 agent，不让整个请求失败
        payload.pop("agent", None)
        r = await client.post(f"{sidecar_base()}/api/session", json=payload)
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
    r = await client.post(f"{sidecar_base()}/api/session/{sid}/prompt", json=payload)
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
        r = await client.get(f"{sidecar_base()}/api/session/{sid}/message")
        if r.status_code >= 400:
            return None
        for it in (r.json() or {}).get("data") or []:
            if isinstance(it, dict) and it.get("id") == user_msg_id:
                return int(((it.get("time") or {}).get("created") or 0)) or None
    except Exception:
        return None
    return None


async def reject_pending_permissions(client: httpx.AsyncClient, sid: str) -> int:
    """拒绝该会话下所有挂起的工具权限请求，返回处理条数。

    为什么需要：CLI 默认 `permission=ask`，而客户端 system prompt（编码 agent 模板等）
    会诱导模型调用 bash/glob 等工具。无人值守时权限请求永远无人批准 → 该条 assistant
    消息停在 `time.completed` 缺失的状态 → 网关侧只能等到超时（实测 180s 后报
    "opencode sidecar 等待回复超时"）。主动 reject 后 CLI 立刻收到工具错误，
    模型转而用文本作答并正常 finish，请求回到秒级。
    """
    n = 0
    try:
        r = await client.get(f"{sidecar_base()}/api/session/{sid}/permission")
        if r.status_code >= 400:
            return 0
        for p in (r.json() or {}).get("data") or []:
            pid = (p or {}).get("id") if isinstance(p, dict) else None
            if not pid:
                continue
            try:
                rr = await client.post(
                    f"{sidecar_base()}/api/session/{sid}/permission/{pid}/reply",
                    json={"reply": "reject"})
                if rr.status_code < 400:
                    n += 1
                    logger.info("opencode sidecar: 已拒绝挂起权限 %s (%s %s)",
                                pid, p.get("action"), p.get("resources"))
            except Exception as e:
                logger.debug("opencode sidecar 拒绝权限失败 %s: %s", pid, e)
    except Exception as e:
        logger.debug("opencode sidecar 查权限失败: %s", e)
    return n


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


def _turn_state(items: list, since_ms: int) -> dict:
    """归纳「本轮」assistant 消息的状态。

    一轮提问可能产生**多条** assistant 消息：模型先调工具（finish=tool-calls，正文可能为空），
    CLI 执行/拒绝后模型再产出最终作答（finish=stop）。所以不能只看第一条，必须整轮归并。
    """
    turn = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("type") != "assistant":
            continue
        created = ((it.get("time") or {}).get("created") or 0)
        if created and created < since_ms:
            continue        # 属于更早的对话，不认
        turn.append(it)
    pending = [it for it in turn if not ((it.get("time") or {}).get("completed"))]
    last = turn[-1] if turn else None
    # 逐条取「该条是否有正文」，末尾优先
    texts = []
    for it in turn:
        t, _ = _content_to_text(it.get("content"))
        if t:
            texts.append(t)
    return {
        "turn": turn,
        "pending": pending,       # 仍在流式填充的（缺 time.completed）
        "last": last,
        "finish": (last or {}).get("finish"),
        "texts": texts,
        "error": next((it.get("error") for it in turn if it.get("error")), None),
    }


def _pick_text(st: dict) -> tuple[str, str]:
    """从整轮里挑出正文与思考内容。

    优先用**最后一条有正文的**消息（模型收尾的作答才是用户要的答案）；
    整轮都没有正文时回退到拼接（例如全部被工具调用占满的失败轮）。
    """
    turn = st.get("turn") or []
    for it in reversed(turn):
        t, r = _content_to_text(it.get("content"))
        if t:
            return t, r
    # 没有正文：把所有 reasoning 拼起来（至少能给出可诊断的内容）
    r_all = []
    for it in turn:
        _, r = _content_to_text(it.get("content"))
        if r:
            r_all.append(r)
    return "", "\n".join(r_all)


def _merge_turn(st: dict) -> dict:
    """把整轮 assistant 消息合并成一条「虚拟 assistant 消息」。

    一轮提问可能产出多条 assistant（先 tool-calls 后 stop），网关只能回一条，
    故正文取最后一条有正文的（收尾作答），用量取「各腿 output 之和 + 最大 input」。
    """
    turn = st.get("turn") or []
    if not turn:
        return {}
    text, reasoning = _pick_text(st)
    merged = dict(turn[-1])
    content = []
    if reasoning:
        content.append({"type": "reasoning", "text": reasoning})
    if text:
        content.append({"type": "text", "text": text})
    merged["content"] = content
    merged["_turn_count"] = len(turn)
    merged["_turn_finish"] = st.get("finish")
    out_sum = 0
    in_max = 0
    for it in turn:
        tk = it.get("tokens") or {}
        out_sum += int(tk.get("output") or 0)
        in_max = max(in_max, int(tk.get("input") or 0))
    merged["tokens"] = {"input": in_max, "output": out_sum}
    # 已收尾（stop）才算 stop；退回中间态时按 stop 报给客户端，避免下游误判为未完成
    merged["finish"] = "stop"
    return merged


async def interrupt_session(client: httpx.AsyncClient, sid: str) -> bool:
    """中断该会话正在进行的生成，返回是否被接受。

    为什么需要：模型被诱导调用工具而 CLI 权限流程卡住时，该条 assistant 永远不写
    `time.completed`（`reply: reject` 只清掉挂起请求、**并不会**让生成继续）。
    实测 `POST .../interrupt` 能让 CLI 立刻收尾该腿（`finish=tool-calls` + 工具标 error），
    于是我们至少能拿到「已生成的思考/正文」而不是干等到 deadline。
    """
    try:
        r = await client.post(f"{sidecar_base()}/api/session/{sid}/interrupt", json={})
        return r.status_code < 400
    except Exception as e:
        logger.debug("opencode sidecar interrupt 失败: %s", e)
        return False


def _turn_signature(st: dict) -> str:
    """整轮「有没有进展」的指纹：腿的数量 + 各腿完成/失败状态 + 正文思考长度。

    用于判定停滞：指纹长时间不变 = CLI 不会再产出新内容，应尽早收尾而不是空等。
    """
    if not st or not st.get("turn"):
        return ""
    parts = []
    for it in st["turn"]:
        tm = it.get("time") or {}
        text, reason = _content_to_text(it.get("content"))
        tools = ",".join(
            "%s:%s" % (c.get("name"), ((c.get("state") or {}).get("status")))
            for c in (it.get("content") or []) if isinstance(c, dict) and c.get("type") == "tool")
        parts.append("%s|%s|%s|%d|%d|%s|%s" % (
            it.get("id"), tm.get("created"), tm.get("completed"),
            len(text), len(reason), it.get("finish"), tools))
    return ";".join(parts)


async def _await_assistant_state(client: httpx.AsyncClient, sid: str, *, since_ms: int,
                                 poll_interval: float = None, deadline_s: float = None) -> dict:
    """轮询到「本轮回复彻底结束」，返回整轮状态 dict（见 _turn_state）。

    实测坑（四条，缺一条就会出问题）：
    1) assistant 消息**逐步填充**——先出现 time.created 与空的 reasoning part，随后才补
       text 并写入 time.completed。只判「消息存在」会拿到空回复。
    2) 偶发把**历史会话内容**带出（首轮实测见过返回无关的 "## Intuition ... linked list"），
       故 assistant 必须 created ≥ 本次提问时间。
    3) **工具调用会占满整轮**：客户端 system prompt 若是编码 agent 模板，模型会去调工具。
       agent 已把工具权限全 deny（减少发生），但一旦发生，腿会停在 `finish=tool-calls`
       且正文为空——CLI 会继续产出下一条腿，需要整轮归并（见 _merge_turn）。
    4) **权限挂起是最坏情况**：腿永远缺 time.completed。实测 `reply: reject` 只清掉挂起请求、
       **并不会让生成继续**；只有 `interrupt` 能让它收尾。故这里用「停滞检测 + interrupt
       收尾」，确保任何情况下都能在有限时间内返回，而不是干等到 deadline。
    """
    interval = poll_interval if poll_interval is not None else sidecar_poll_interval()
    timeout = deadline_s if deadline_s is not None else sidecar_timeout()
    deadline = time.monotonic() + timeout
    reject_on = auto_reject_tools()
    best = None
    last_sig = None
    last_change = time.monotonic()
    interrupted = False
    # 停滞容忍：默认 20s（可用 opencode_bridge.stall_grace_seconds 调）；
    # 轮询极快（测试场景）时按间隔缩放但不低于 1s，避免测试要等 20s。
    stall_grace = stall_grace_seconds() if interval >= 0.2 else min(2.0, max(0.2, interval * 15))
    _STOP_FINISH = {"stop", "length", "content_filter", "error", "aborted", "cancelled"}
    while time.monotonic() < deadline:
        try:
            r = await client.get(f"{sidecar_base()}/api/session/{sid}/message")
            if r.status_code < 400:
                st = _turn_state((r.json() or {}).get("data") or [], since_ms)
                if st["turn"]:
                    best = st
                # 无人值守：先尝试清掉挂起的工具权限（有些版本会让生成继续）
                if reject_on and st["turn"]:
                    await reject_pending_permissions(client, sid)
                if st["turn"]:
                    sig = _turn_signature(st)
                    if sig != last_sig:
                        last_sig = sig
                        last_change = time.monotonic()
                    elif not st["pending"] and st["finish"] in _STOP_FINISH:
                        return st               # 正常收尾（含 error）
                    elif time.monotonic() - last_change > stall_grace:
                        # 停滞：先 interrupt 收尾，拿回已有内容
                        if not interrupted and await interrupt_session(client, sid):
                            interrupted = True
                            last_change = time.monotonic()
                            logger.info("opencode sidecar: 本轮停滞，已 interrupt 收尾")
                        else:
                            # interrupt 也没进展（或已被拒）→ 不再空等
                            logger.info(
                                "opencode sidecar: 本轮停在 finish=%s 且 %ss 无进展，返回已有内容",
                                st.get("finish"), int(stall_grace))
                            return st
                if not st["pending"] and st["finish"] in _STOP_FINISH and st["turn"]:
                    return st
        except Exception as e:      # 轮询期瞬时错误不致命
            logger.debug("opencode sidecar poll: %s", e)
        await asyncio.sleep(interval)
    if best is not None and best.get("turn"):
        # 超时但已有内容：返回已拿到的（调用方按 finish/正文判定完整性）
        logger.warning("opencode sidecar: 等待回复超时（%.0fs），返回已完成的部分内容", timeout)
        return best
    raise TimeoutError(f"opencode sidecar 等待回复超时（{timeout:.0f}s）")


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
    if not prompt_text.strip():
        # 空 prompt 会让 CLI 自由发挥（实测回寒暄/无关内容），必须显式失败而非静默发出
        raise RuntimeError(
            "opencode sidecar: 无法从 messages 提取任何文本（检查消息格式："
            "支持 dict 与 pydantic ChatMessage）")
    own = client is None
    c = client or httpx.AsyncClient(timeout=sidecar_timeout())
    try:
        sid = await _create_session(c, provider_id, model_id)
        t0 = int(time.time() * 1000)
        user_msg_id = await _post_prompt(c, sid, prompt_text, files=files)
        # 用 sidecar 分配的用户消息 id 的时间戳做归属基线（比本地 t0 更贴近服务端时钟）
        since = await _user_msg_created_at(c, sid, user_msg_id) or t0
        msg, err = await _await_turn(c, sid, since_ms=since)
        _close = True
        try:
            if err:
                raise RuntimeError(
                    f"opencode sidecar 上游报错: {json.dumps(err, ensure_ascii=False)[:300]}")
            text, reasoning = _content_to_text(msg.get("content"))
            if not text.strip() and not reasoning.strip():
                # 空结果必须显式失败：走到这里说明整轮只有工具调用/空壳，
                # 若按 200 返回空内容，combo/auto 会把它当成「成功但没话说」而锁定该候选。
                turn_finish = msg.get("_turn_finish")
                why = ("整轮只发起工具调用、模型未给出任何文本"
                       if turn_finish == "tool-calls" else "侧车未返回任何文本")
                raise RuntimeError(
                    f"opencode sidecar 空回复（{why}，finish={turn_finish}，"
                    f"腿数={msg.get('_turn_count')}）")
            return _to_openai_response(msg, model_id, prompt_text)
        finally:
            if _close:
                await _close_session(c, sid)
    finally:
        if own:
            await c.aclose()


async def _await_turn(client: httpx.AsyncClient, sid: str, *, since_ms: int,
                      poll_interval: float = None, deadline_s: float = None):
    """等整轮结束，并把多腿 assistant 合并成一条可回给客户端的结果。"""
    interval = poll_interval if poll_interval is not None else sidecar_poll_interval()
    deadline = time.monotonic() + (deadline_s if deadline_s is not None else sidecar_timeout())
    st = await _await_assistant_state(client, sid, since_ms=since_ms,
                                      poll_interval=interval, deadline_s=deadline - time.monotonic())
    return _merge_turn(st), st.get("error")


async def _await_assistant(client: httpx.AsyncClient, sid: str, *, since_ms: int,
                           poll_interval: float = None, deadline_s: float = None):
    """兼容旧签名：返回 (合并后的消息, 错误)。"""
    interval = poll_interval if poll_interval is not None else sidecar_poll_interval()
    st = await _await_assistant_state(client, sid, since_ms=since_ms,
                                      poll_interval=interval, deadline_s=deadline_s)
    return _merge_turn(st), st.get("error")


async def _close_session(client: httpx.AsyncClient, sid: str) -> None:
    """尽力关闭 session（避免 sidecar 里会话堆积）；失败不影响结果。

    实测坑：删除接口**只挂在无 `/api` 前缀的路径**上（`DELETE /session/{sid}`）。
    带 `/api` 的 `/api/session/{sid}` 会被 Web UI 兜底路由吃掉并回 200 HTML，
    看起来「成功」但会话其实还在（50 个会话删完仍是 50 个）。故此处必须用无前缀路径。
    """
    for path in (f"/session/{sid}", f"/api/session/{sid}"):
        try:
            r = await client.delete(sidecar_base() + path)
            if r.status_code < 400 and "json" in (r.headers.get("content-type") or ""):
                return
            if r.status_code < 400 and not (r.text or "").lstrip().startswith("<"):
                return
        except Exception:
            continue


def flatten_messages(messages: list) -> str:
    """OpenAI messages → 单段文本（保留角色语义，供 CLI prompt 使用）。

    行为语义由 sidecar 的 `aigate` agent（SIDECAR_AGENT）负责——那里定义了
    「直接作答、不寒暄/不自述/不追问」的系统提示词。这里只做消息拼接，
    不再叠加前置指令（避免与 agent 提示词重复、互相干扰）。
    """
    return _flatten_body(messages)


def _as_dict(m):
    """兼容 dict 与 Pydantic 模型（ChatMessage）。

    踩坑：executor 传入的是 pydantic `ChatMessage` 对象，不是 dict；
    早先只判 isinstance(dict) 会把**所有消息静默跳过** → 空 prompt → CLI 自由发挥
    （实测表现为回复寒暄或完全无关内容，极难定位）。
    """
    if isinstance(m, dict):
        return m
    dump = getattr(m, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except Exception:
            return {}
    # 兜底：按属性取
    return {"role": getattr(m, "role", None), "content": getattr(m, "content", None)}


def _flatten_body(messages: list) -> str:
    chunks = []
    for raw in messages or []:
        m = _as_dict(raw)
        if not m:
            continue
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, list):
            # typed blocks → 拼接文本（元素也可能是 pydantic 对象）
            parts = []
            for p in content:
                pd = _as_dict(p) if not isinstance(p, str) else {"type": "text", "text": p}
                if pd.get("type") in ("text", "input_text") or "text" in pd:
                    parts.append(str(pd.get("text") or ""))
            text = "".join(parts)
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


# ─────────────────────────── CLI agent 定义：网关无人值守形态 ───────────────────────────
# 为什么必须写这份配置：
#   1) CLI 默认 agent 是「编码助手」人格，直出语义不符（实测回 "Hi! I'm ready to help
#      with your workspace at ..."）；
#   2) 更致命的是默认 permission=ask —— 客户端 system prompt 诱导模型调工具时，
#      权限请求在无人值守场景永远无人批准，消息永久挂起 → 网关只能等到超时。
# 这里把工具一律 deny（模型立刻收到工具错误，转而用文本作答），并约束人格。
#
# 注：不能改成「tools 全 false」——实测那样 CLI 会返回**空回复**（工具列表为空时
# 模型行为异常）。permission=deny 则保留工具声明、执行被拒，回复正常。
_BRIDGE_PROMPT = (
    "You are a direct assistant behind an API gateway. Answer the user's request "
    "directly and completely in the same language they used. Do not greet. Do not "
    "describe yourself. Do not mention tools, workspaces, or files. Do not ask "
    "clarifying questions unless the request is truly impossible to answer. "
    "Tools are unavailable in this environment; always answer from your own knowledge "
    "as text and never wait for tool output."
)

# 已知工具名（CLI 1.18.32 的 /experimental/tool/ids 实测值）
_BRIDGE_TOOLS = ("bash", "read", "glob", "grep", "edit", "write", "task", "webfetch",
                 "websearch", "todowrite", "skill", "apply_patch", "question")


def bridge_agent_config(agent_name: str = None) -> dict:
    """生成网关专用 agent 的 CLI 配置。

    工具权限用**单字符串 "deny"**（覆盖所有工具，含未来新增），而不是逐个工具名的对象：
    实测两者都能消除「权限挂起」，但字符串形式不依赖工具名清单，上游加工具也不会漏。
    注意**不要**用 `tools: {x: false}`——实测那样 CLI 会返回**空回复**。
    """
    name = agent_name or sidecar_agent() or "aigate"
    return {
        name: {
            "description": "AIGate gateway chat-only agent",
            "mode": "primary",
            "prompt": _BRIDGE_PROMPT,
            "permission": "deny",
        }
    }


def _cli_config_path() -> str:
    """CLI 全局配置路径（XDG 规范；与 CLI 自己解析的位置一致）。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "opencode", "opencode.json")


def _cli_config_problem(cfg: dict, agent_name: str) -> str:
    """返回需要修正的原因；已正确时返回空串。"""
    a = ((cfg.get("agent") or {}).get(agent_name) or {}) if isinstance(cfg, dict) else {}
    if not a:
        return "agent 未定义"
    if a.get("mode") != "primary":
        return "mode 非 primary"
    perm = a.get("permission")
    # 期望：单字符串 deny，或「所有已声明工具都 deny」的对象形式
    if perm != "deny":
        if not isinstance(perm, dict) or any(v != "deny" for v in perm.values()):
            return "工具权限未全部 deny"
    if (a.get("prompt") or "").strip() != _BRIDGE_PROMPT:
        return "提示词已过期"
    if a.get("tools"):
        # tools 显式关闭会导致空回复，必须清掉
        return "存在 tools 开关（会导致空回复）"
    return ""


def ensure_bridge_agent_config(agent_name: str = None, path: str = None) -> tuple[bool, str]:
    """确保 CLI 侧存在网关专用 agent（幂等；只增改该 agent，不动其它配置）。

    返回 (是否已就绪, 说明)。任何异常都吞掉并返回说明——启动流程不应因此失败。
    """
    name = agent_name or sidecar_agent() or "aigate"
    p = path or _cli_config_path()
    try:
        cfg = {}
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                try:
                    cfg = json.loads(raw)
                except Exception:
                    return False, f"{p} 不是合法 JSON，未改动（请手工检查）"
        if not isinstance(cfg, dict):
            return False, f"{p} 顶层不是对象，未改动"
        problem = _cli_config_problem(cfg, name)
        if not problem:
            return True, f"agent '{name}' 已就绪"
        agent = dict(cfg.get("agent") or {})
        agent[name] = bridge_agent_config(name)[name]
        cfg["agent"] = agent
        cfg.setdefault("$schema", "https://opencode.ai/config.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True, f"已写入 agent '{name}'（{problem}）"
    except Exception as e:
        return False, f"写入 CLI 配置失败: {e}"


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
