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


def context_overflows(model: Any, est_tokens: int, reserve: int = _OUTPUT_RESERVE_TOKENS) -> bool:
    """估算输入是否装不进模型的上下文窗口（预留输出空间）。

    context_length <= 0 视为未知窗口，不拦截（放行由上游兜底）。
    """
    window = int(getattr(model, "context_length", 0) or 0)
    if window <= 0:
        return False
    return est_tokens + max(reserve, 0) > window
