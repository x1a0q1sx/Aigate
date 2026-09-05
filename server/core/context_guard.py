"""上下文窗口守护：请求 token 估算 + 超限错误识别。

解决的问题：
1. 超长请求打到小窗口模型 → 上游 400 → 模型被误判故障进冷却。
   → 路由前用估算值预检，直接跳过装不下的候选（不打上游、不进冷却）。
2. 即便漏网（估算偏差/未登记窗口），上游返回的 context 类 400 也不应触发冷却。
   → is_context_error() 识别此类错误，mark_cooling 前先过这道闸。

估算刻意保守偏大（宁可错杀跳过，也不打出确定失败请求）；真值由上游 usage 校准不了，
客户端可接受的误差在 20% 以内不影响跳过决策的正确性。
"""
import json
import re
from typing import Any

# 上游各家 context 超限报错的特征串（小写匹配；含国内公益站常见中文报错）
_CONTEXT_ERROR_PATTERNS = [
    # 英文（OpenAI / Anthropic / Gemini / 各兼容站）
    "context length",
    "context window",
    "maximum context",
    "max context",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "request too large",
    "reduce the length",
    "exceeds the maximum",
    "exceeds your",
    "maximum_number_of_tokens",
    "max_tokens is too large",
    "input length and `max_tokens`",
    "input tokens exceed",
    "longer than the model's context",
    "requested tokens exceed",
    "context_length_exceeded",
    "content filter: too long",
    # 中文（new-api / 国内公益站常见报错）
    "内容超长",
    "内容过长",
    "输入超长",
    "输入过长",
    "请求过长",
    "提示词过长",
    "超出上下文",
    "超过上下文",
    "上下文长度",
    "长度超限",
    "长度限制",
    "超出长度",
    "超出最大",
    "超过最大长度",
    "token 超限",
    "token超限",
    "过长，请",
    "超长，请",
]

_CJK_RANGES = (
    (0x2E80, 0x9FFF),    # CJK 部首/符号/汉字
    (0x3040, 0x30FF),    # 假名
    (0xAC00, 0xD7AF),    # 韩文
    (0xF900, 0xFAFF),    # 兼容汉字
    (0xFF00, 0xFFEF),    # 全角形式
)


def _is_cdp(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _CJK_RANGES)


def estimate_text_tokens(text: str) -> int:
    """混合中英文文本的 token 估算：CJK 约 1 字/token，其余约 4 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cdp(ch))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                t = p.get("text") or p.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
                elif isinstance(t, list):
                    parts.append(json.dumps(t, ensure_ascii=False))
                # 图片/音频部件按固定成本在调用方计入
        return " ".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return str(content or "")


def estimate_request_tokens(request: Any) -> int:
    """估算一次 ChatCompletionRequest 的输入 token 数（含 tools 定义）。"""
    total = 0
    for m in (getattr(request, "messages", None) or []):
        content = getattr(m, "content", None)
        total += estimate_text_tokens(_content_to_text(content))
        # 多模态：每个图片部件按 ~800 token 保守计（各模型 700-1600 不等）
        if isinstance(content, list):
            total += 800 * sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
        rc = getattr(m, "reasoning_content", None)
        if rc:
            total += estimate_text_tokens(str(rc))
        tc = getattr(m, "tool_calls", None)
        if tc:
            total += estimate_text_tokens(json.dumps(tc, ensure_ascii=False, default=str))
    tools = getattr(request, "tools", None)
    if tools:
        total += estimate_text_tokens(json.dumps(tools, ensure_ascii=False, default=str))
    return max(total, 16)


def is_context_error(err_text: Any) -> bool:
    """判断错误文本是否为上下文超限类（不应让模型进冷却）。"""
    if not err_text:
        return False
    t = str(err_text).lower()
    return any(p in t for p in _CONTEXT_ERROR_PATTERNS)


_OUTPUT_RESERVE_TOKENS = 1024


def context_overflows(model: Any, est_tokens: int, reserve: int = _OUTPUT_RESERVE_TOKENS,
                      observed_limit: int = 0) -> bool:
    """估算输入是否装不进模型的上下文窗口（预留输出空间）。

    - context_length <= 0 视为未知窗口，不拦截（放行由上游兜底）
    - observed_limit > 0 时取 min(登记窗口, 观察到的实际上限)——同一模型在
      不同服务商上的真实限制可能远小于标称窗口
    """
    window = int(getattr(model, "context_length", 0) or 0)
    if observed_limit and observed_limit > 0:
        window = min(window, int(observed_limit)) if window > 0 else int(observed_limit)
    if window <= 0:
        return False
    return est_tokens + max(reserve, 0) > window


# ── P1-5 动态估算系数：按模型历史 (估算值 → 上游真实 prompt_tokens) 校准 ──
_factor_cache: dict = {}          # (provider_id, model_id_str) -> (factor, ts)
_FACTOR_TTL = 600                 # 10 分钟
_FACTOR_CLAMP = (0.4, 4.0)        # 防止异常样本把系数拉飞


async def get_estimate_factor(session, provider_id: int, model_id_str: str) -> float:
    """该服务商+模型的历史估算修正系数（最近 20 条有估算+真实值的样本：AVG(prompt/est)）。

    带 TTL 内存缓存；无样本/查询失败返回 1.0。估算偏小时系数 >1（常见：工具调用、
    多模态、厂商 tokenizer 差异），偏大时 <1。
    """
    import time as _time
    key = (provider_id, model_id_str)
    cached = _factor_cache.get(key)
    now = _time.time()
    if cached and now - cached[1] < _FACTOR_TTL:
        return cached[0]
    factor = 1.0
    try:
        from sqlalchemy import text as _text
        row = (await session.execute(_text(
            "SELECT AVG(prompt_tokens * 1.0 / est_prompt_tokens) FROM ("
            "  SELECT prompt_tokens, est_prompt_tokens FROM request_logs"
            "  WHERE routed_provider_id = :pid AND routed_model = :mid"
            "    AND est_prompt_tokens > 0 AND prompt_tokens > 0 AND is_health_check = 0"
            "  ORDER BY id DESC LIMIT 20"
            ")"
        ), {"pid": provider_id, "mid": model_id_str})).first()
        if row and row[0]:
            factor = max(_FACTOR_CLAMP[0], min(_FACTOR_CLAMP[1], float(row[0])))
    except Exception:
        factor = 1.0
    _factor_cache[key] = (factor, now)
    return factor


async def record_context_overflow(model_pk: int, est_tokens: int) -> None:
    """上游返回上下文超限错误时学习：记录该模型的观察窗口上限（取历史最小值）。

    同一模型在不同服务商/部署上的真实限制可能远小于标称窗口；本记录直接收紧
    该模型的预检窗口（observed_context_limit），避免每次都打上去吃一次 400。
    """
    if not model_pk or est_tokens <= 0:
        return
    try:
        from sqlalchemy import text as _text
        from server.db import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            await db.execute(_text(
                "UPDATE models SET observed_context_limit = :est "
                "WHERE id = :pk AND (observed_context_limit IS NULL OR observed_context_limit > :est)"
            ), {"est": int(est_tokens), "pk": int(model_pk)})
            await db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("record_context_overflow failed: %s", e)
