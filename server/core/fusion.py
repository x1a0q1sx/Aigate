"""Fusion 组合策略：并行 fan-out 全部候选 + judge 模型合成最终答案。

设计：docs/superpowers/specs/2026-09-11-fusion-strategy-design.md
关键语义：
- ≥2 个候选成功才调 judge（成本 N+1）；恰好 1 个成功直接返回（省一次钱）
- 全部失败 → FusionAllFailed（调用方转 502 规范错误对象）
- judge 缺省 = 成功候选中智力评分最高（intelligence_static 体系）；可在
  combo.fusion_config.judge 指定（须为组合内候选）
- 单候选默认超时 30s（combo.fusion_config.timeout_seconds 可调）；
  上下文预检/冷却跳过由调用方注入 precheck 回调
- judge 失败/空答案 → 降级为最长的成功回答，响应带 aigate_fusion.fallback 标记
"""
import asyncio
import fnmatch
import time
from typing import Optional

from server.schemas.chat import ChatMessage

DEFAULT_MAX_TARGETS = 6
DEFAULT_TIMEOUT = 30.0
_INTEL_TTL = 600.0
_intel_cache = {"t": 0.0, "rows": []}


class FusionAllFailed(Exception):
    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__(f"fusion: all {len(attempts)} candidates failed")


class _SkipCandidate(Exception):
    pass


def _extract_text(result) -> Optional[str]:
    """OpenAI 非流式响应 → 回答正文（无实质正文返回 None）。"""
    try:
        choices = (result or {}).get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content")
    except AttributeError:
        return None
    if isinstance(content, str) and content.strip():
        return content
    return None


async def default_call(db, provider, model, request, timeout: float):
    """生产路径单候选调用：统一凭证解析 + 非流式（free/oauth/atomcode/标准一个入口）。"""
    from server.core.credential_resolver import resolve_credential_async, call_via
    rc = await resolve_credential_async(provider, model, db)
    if not rc.ok:
        raise RuntimeError(rc.error or "no credential")
    return await asyncio.wait_for(call_via(rc, request, provider, model), timeout)


async def _intel_rows(db):
    now = time.monotonic()
    if now - _intel_cache["t"] < _INTEL_TTL:
        return _intel_cache["rows"]
    try:
        from sqlalchemy import select
        from server.models.intelligence import IntelligenceStatic
        rows = (await db.execute(
            select(IntelligenceStatic.pattern, IntelligenceStatic.score)
        )).all()
        _intel_cache["rows"] = [(str(p).lower(), float(sc or 0)) for p, sc in rows]
        _intel_cache["t"] = now
    except Exception:
        pass
    return _intel_cache["rows"]


def _model_score(model_id: str, rows) -> float:
    mid = (model_id or "").lower()
    best = 0.0
    for pattern, sc in rows:
        if not pattern:
            continue
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(mid, pattern):
                best = max(best, sc)
        elif pattern in mid:
            best = max(best, sc)
    return best


def _judge_request(base, successes):
    """judge 请求 = 原始对话 + 一条合成指令（保留上下文，成本集中在指令消息）。"""
    parts = [
        "【AIGate Fusion】以下是多个模型对同一用户请求的独立回答。请综合成一份最终回答：",
        "- 保留信息量最大、相互印证正确的内容；",
        "- 对存在分歧的点，指出分歧并给出更可信的一方；",
        "- 只输出最终回答本身，不要复述来源列表。",
        "",
    ]
    for i, s in enumerate(successes, 1):
        text = s["text"]
        if len(text) > 8000:
            text = text[:8000] + "\n...(truncated)"
        parts.append(f"【模型{i}：{s['target']['full_id']}】\n{text}\n")
    msgs = list(base.messages or []) + [ChatMessage(role="user", content="\n".join(parts))]
    return base.model_copy(update={
        "messages": msgs, "stream": False, "temperature": 0,
        "reasoning_effort": None, "reasoning": None,
    })


def _cfg_of(combo) -> dict:
    try:
        return (combo and getattr(combo, "fusion_config", None)) or {}
    except Exception:
        return {}


async def run_fusion(db, targets, request, *, combo=None, call_fn=None,
                     precheck=None, on_attempt=None):
    """执行一次 fusion。返回 (OpenAI dict 响应, meta)；全败抛 FusionAllFailed。

    meta = {sources, fused, judge:(provider, model)|None, attempts}
    targets 为 resolve_combo_targets 的结果（含 provider/model ORM 与 full_id）。
    call_fn(db, provider, model, request) 可注入（测试用）。
    """
    cfg = _cfg_of(combo)
    max_t = int(cfg.get("max_targets") or DEFAULT_MAX_TARGETS)
    timeout = float(cfg.get("timeout_seconds") or DEFAULT_TIMEOUT)
    base = request.model_copy(update={"stream": False})
    chosen = list(targets)[: max(1, min(max_t, len(targets)))]

    attempts, successes = [], []
    for idx, t in enumerate(chosen):
        full_id = t.get("full_id") or "?"
        try:
            if precheck:
                reason = await precheck(t["provider"], t["model"])
                if reason:
                    raise _SkipCandidate(str(reason))
            if call_fn is not None:
                result = await asyncio.wait_for(
                    call_fn(db, t["provider"], t["model"], base), timeout)
            else:
                result = await default_call(db, t["provider"], t["model"], base, timeout)
            text = _extract_text(result)
            if text is None:
                raise RuntimeError("empty_response")
            successes.append({"target": t, "result": result, "text": text})
            attempts.append({"attempt": idx, "target": full_id, "ok": True, "chars": len(text)})
            if on_attempt:
                on_attempt(t, True, None)
        except Exception as e:
            reason = f"{type(e).__name__}: {str(e)[:200]}"
            attempts.append({"attempt": idx, "target": full_id, "ok": False, "error": reason})
            if on_attempt:
                on_attempt(t, False, reason)

    if not successes:
        raise FusionAllFailed(attempts)

    # 恰好 1 个成功：直接返回，不调 judge（省成本）
    if len(successes) == 1:
        s = successes[0]
        out = {**s["result"], "model": request.model,
               "aigate_fusion": {"fused": False, "sources": [s["target"]["full_id"]]}}
        return out, {
            "sources": [s["target"]["full_id"]], "fused": False,
            "judge": (s["target"]["provider"].name, s["target"]["model"].model_id),
            "attempts": attempts,
        }

    # ── judge 选择：配置指定（须在候选内）→ 否则成功候选中智力最高 ──
    judge_s = None
    jq = cfg.get("judge") or {}
    if jq.get("provider") and jq.get("model_id"):
        jfull = f"{jq['provider']}/{jq['model_id']}"
        for s in successes:
            if s["target"]["full_id"] == jfull:
                judge_s = s
                break
        if judge_s is None:
            for t in chosen:
                if t["full_id"] == jfull:
                    judge_s = {"target": t, "result": None, "text": ""}
                    break
    if judge_s is None:
        rows = await _intel_rows(db)
        judge_s = sorted(successes, key=lambda s: -_model_score(
            s["target"]["model"].model_id, rows))[0]
    jt = judge_s["target"]

    jreq = _judge_request(base, successes)
    try:
        if call_fn is not None:
            jres = await asyncio.wait_for(
                call_fn(db, jt["provider"], jt["model"], jreq), timeout + 15)
        else:
            jres = await default_call(db, jt["provider"], jt["model"], jreq, timeout + 15)
        jtext = _extract_text(jres)
        if jtext is None:
            raise RuntimeError("judge_empty_response")
    except Exception as e:
        # judge 失败 → 降级最长的成功回答
        fallback_note = f"judge_failed: {type(e).__name__}: {str(e)[:150]}"
        longest = max(successes, key=lambda s: len(s["text"]))
        lt = longest["target"]
        attempts.append({"attempt": "judge", "target": lt["full_id"], "ok": False,
                         "error": fallback_note})
        if on_attempt:
            on_attempt(lt, False, fallback_note)
        out = {**longest["result"], "model": request.model,
               "aigate_fusion": {"fused": False, "fallback": fallback_note,
                                 "sources": [s["target"]["full_id"] for s in successes]}}
        return out, {
            "sources": [s["target"]["full_id"] for s in successes], "fused": False,
            "judge": (lt["provider"].name, lt["model"].model_id), "attempts": attempts,
        }

    attempts.append({"attempt": "judge", "target": jt["full_id"], "ok": True, "chars": len(jtext)})
    out = {**jres, "model": request.model,
           "aigate_fusion": {"fused": True,
                             "sources": [s["target"]["full_id"] for s in successes]}}
    return out, {
        "sources": [s["target"]["full_id"] for s in successes], "fused": True,
        "judge": (jt["provider"].name, jt["model"].model_id), "attempts": attempts,
    }
