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


# ── v4.3 能力感知路由：请求画像 + 模态闸 ──

# 长上下文判定：估算输入超过该值即视为「长上下文请求」，排序偏向大窗口模型
LONG_CONTEXT_THRESHOLD_TOKENS = 32768


class RequestProfile:
    """一次请求的能力需求画像（由 analyze_request 产出，全程只算一次）。"""
    __slots__ = ("est_tokens", "has_image", "has_audio")

    def __init__(self, est_tokens: int, has_image: bool = False, has_audio: bool = False):
        self.est_tokens = est_tokens
        self.has_image = has_image
        self.has_audio = has_audio

    @property
    def is_long(self) -> bool:
        return self.est_tokens >= LONG_CONTEXT_THRESHOLD_TOKENS


def _part_media(p: Any) -> str:
    """OpenAI/Anthropic 消息部件 → 媒体类型（""=纯文本）。"""
    if not isinstance(p, dict):
        return ""
    t = (p.get("type") or "").lower()
    if t in ("image_url", "image"):
        return "image"
    if t in ("input_audio", "audio"):
        return "audio"
    return ""


def analyze_request(request: Any) -> RequestProfile:
    """估算 token 的同时产出能力需求：是否含图片/音频部件。"""
    est = estimate_request_tokens(request)
    has_image = False
    has_audio = False
    for m in (getattr(request, "messages", None) or []):
        content = getattr(m, "content", None)
        if isinstance(content, list):
            for p in content:
                mt = _part_media(p)
                if mt == "image":
                    has_image = True
                elif mt == "audio":
                    has_audio = True
    return RequestProfile(est, has_image, has_audio)


def media_known_modalities(model: Any):
    """模型的**可信**已知输入模态集合；None = 未知（不参与拦截判断）。

    可信 = openrouter / provider / manual 来源的 input_modalities 列表。
    "inferred"（名称启发式）不算可信：它只能证明「支持某模态」（用于排序加分），
    不能证明「不支持另一模态」——拿它做硬拦截会把误判变成事故。
    supports_vision=True 同理只是正向证据，不构成闭合集合。
    """
    if (getattr(model, "capability_source", "") or "") == "inferred":
        return None
    im = getattr(model, "input_modalities", None)
    if isinstance(im, list) and im:
        return {str(x).lower() for x in im}
    return None


def media_mismatch_reason(model: Any, profile: RequestProfile) -> str:
    """返回拦截原因（非空=跳过该候选），空串=放行。

    安全边界：只有「可信来源的闭合模态集合」（media_known_modalities）才参与拦截——
    生产近 2/3 模型无模态标注，按「未知即不支持」拦截会造成大面积不可用。
    真打错了由上游 4xx/5xx 兜底（context 类错误不进冷却）。
    """
    if not profile or (not profile.has_image and not profile.has_audio):
        return ""
    known = media_known_modalities(model)
    if known is None:
        return ""
    if profile.has_image and "image" not in known:
        return "需要图片输入，模型模态已知不含 image"
    if profile.has_audio and "audio" not in known:
        return "需要音频输入，模型模态已知不含 audio"
    return ""


def _positive_modalities(model: Any) -> set:
    """模型「支持某模态」的正向证据（可信集合 + inferred + supports_vision 布尔）。"""
    pos = set()
    im = getattr(model, "input_modalities", None)
    if isinstance(im, list):
        pos |= {str(x).lower() for x in im}
    if getattr(model, "supports_vision", False):
        pos.add("image")
    return pos


def capability_preference(model: Any, profile: RequestProfile) -> float:
    """排序偏好加分（非硬闸）：能力匹配的候选排前面。

    - 多模态请求：已知/推断支持对应模态的模型 +10；
    - 长上下文请求：窗口档位递进加分（≥1M +14，≥256K +10，≥128K +6，≥32K +2）；
      未知窗口（context_length<=0）不加分也不惩罚（1653/2560 模型窗口未标定）。
    上限 ~24 分，小于智力分/稳定性分的量级差，只影响"同档内"的相对顺序。
    """
    if not profile:
        return 0.0
    bonus = 0.0
    if profile.has_image and "image" in _positive_modalities(model):
        bonus += 10.0
    if profile.has_audio and "audio" in _positive_modalities(model):
        bonus += 10.0
    if profile.is_long:
        w = int(getattr(model, "context_length", 0) or 0)
        obs = int(getattr(model, "observed_context_limit", 0) or 0)
        if w > 0 and obs > 0:
            w = min(w, obs)
        elif w <= 0 and obs > 0:
            w = obs
        if w >= 1_000_000:
            bonus += 14.0
        elif w >= 256_000:
            bonus += 10.0
        elif w >= 128_000:
            bonus += 6.0
        elif w >= 32_000:
            bonus += 2.0
    return bonus


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
