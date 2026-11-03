"""思考强度（reasoning_effort）形状错误的识别与自适应降级（2026-10-29）。

**背景（生产实证）**：CodeBuddy 上游对 `reasoning_effort` 的取值做**动态**校验 ——
同一模型在不同账号 / 档位 / 时段下被允许的档位集合会变。生产 2026-10-29 07:33:39
的一次请求（hy4-preview，账号 15944101987）被拒：

    HTTP 400 {"code":11150,
              "msg":"the reasoning effort value is not supported by the current model",
              "extError":{"code":"invalid_reasoning_effort"}}

而同一天 09:00 之后对同一模型跑的标准档位矩阵（minimal/low/medium/high/xhigh/
none/off）、垃圾值、空串、reasoning 块、6.7k 长 prompt、tools、流式全部 200，
唯一被拒的 `max` 报的是 **11133**（invalid request parameters）而非 11150 ——
说明 11150 不是稳定的「形状错误」，而是 (模型 × 当前允许档位集合) 的动态校验，
失败时刻正落在 hy4-preview 的「夜间免费 23:00-08:00」窗口内。

**网关侧的真实缺陷**（本模块负责的两条）：
1. 没有自适应路径：`provider_quirks` 把 effort 原样转发，`adapter_retry` 把 4xx
   一律判为不可重试 → 客户端一旦带了上游当时不认的档位，整个请求直接失败。
2. 形状错误被当成模型故障：`v1_router` 的失败记账对 11150 也
   `mark_failure + mark_cooling(30s)`，把一个完全健康的模型冷却掉，后续请求
   连试都不试。

处置：上游回这类 400 时**剥掉 effort 再试一次**，并把 (上游, 模型, 档位) 记入
进程内抑制表，TTL 内后续请求直接不带该档位（省一次注定失败的往返）；同时把
这类错误标为「请求形状问题」，`v1_router` 据此跳过模型冷却。

抑制表是**有意的短时记忆而非永久禁用**：上游允许集合会随时间变（夜间窗口开关
就是实证），TTL 到期后重新尝试带档位，失败再记，既自愈又不每次都白烧一次往返。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 抑制时长：与模型冷却时长同量级。到期后重新尝试带档位（自愈）。
SUPPRESSION_TTL_SECONDS = 1800

# (上游 host, 模型 id, 档位小写) -> 到期时间戳（monotonic）
_SUPPRESSED: dict[tuple[str, str, str], float] = {}

# 上游原文里的 effort 拒因（大小写不敏感）。中文文案未观测到，先只认英文 + extError。
_EFFORT_REJECTION_MARKERS = (
    "invalid_reasoning_effort",
    "reasoning effort value is not supported",
    "reasoning_effort value is not supported",
    "reasoning effort is not supported",
)

# extError.code 之外的顶层 code 兜底：报文被截断时文案可能丢失，code 还在。
_CODE_RE = re.compile(r'"code"\s*:\s*"?(\d{4,6})"?')
_EFFORT_REJECTION_CODES = frozenset({11150})
# 通用「请求参数不合法」码：CodeBuddy 对超范围档位（如 effort=max）回这个。
# 模型本身是好的 —— 换档位/改参数就好，不该进模型冷却。
_REQUEST_SHAPE_CODES = frozenset({11133})


def _top_code(text: str) -> Optional[int]:
    """报文里的第一个 `"code":NNNNN`（顶层，非 extError 里的字符串码）。"""
    m = _CODE_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def is_reasoning_effort_rejection(error) -> bool:
    """上游是否明确「这个 reasoning_effort 值我这儿不认」。

    命中即可以剥掉 effort 重试（语义上等价于「不指定思考强度」，不是降级内容）。"""
    text = str(error or "")
    low = text.lower()
    if any(m in low for m in _EFFORT_REJECTION_MARKERS):
        return True
    return _top_code(low) in _EFFORT_REJECTION_CODES


def is_request_shape_error(error) -> bool:
    """请求形状/参数错误（不是模型故障）→ 调用方不应计入模型冷却。

    覆盖 effort 类拒因（11150 / invalid_reasoning_effort）与 CodeBuddy 的通用
    参数码 11133：两类都表示「请求发错了，模型是好的」。
    """
    if is_reasoning_effort_rejection(error):
        return True
    return _top_code(str(error or "").lower()) in _REQUEST_SHAPE_CODES


def suppression_key(url_host: str, model_id: str, effort: str) -> tuple[str, str, str]:
    return (str(url_host or "").lower(), str(model_id or ""), str(effort or "").lower())


def is_suppressed(url_host: str, model_id: str, effort: str) -> bool:
    """该 (上游, 模型, 档位) 是否处于抑制期（应直接不带 effort 出站）。"""
    if not effort:
        return False
    key = suppression_key(url_host, model_id, effort)
    until = _SUPPRESSED.get(key)
    if until is None:
        return False
    if time.monotonic() >= until:
        _SUPPRESSED.pop(key, None)
        return False
    return True


def suppress(url_host: str, model_id: str, effort: str,
             ttl: Optional[float] = None) -> None:
    """记录一次「上游不认这个档位」，TTL 内不再带它出站。"""
    if not effort:
        return
    ttl = SUPPRESSION_TTL_SECONDS if ttl is None else max(0.0, float(ttl))
    _SUPPRESSED[suppression_key(url_host, model_id, effort)] = time.monotonic() + ttl
    logger.warning("上游拒绝 reasoning_effort=%s（%s / %s），%d 秒内不再携带该档位",
                   effort, url_host or "?", model_id or "?", int(ttl))


def reset() -> None:
    """清空抑制表（测试用；进程级状态必须逐测试重置，否则跨用例互相影响）。"""
    _SUPPRESSED.clear()


def suppressed_count() -> int:
    """当前抑制中的条目数（不含已过期项）。"""
    now = time.monotonic()
    for k in [k for k, v in _SUPPRESSED.items() if now >= v]:
        _SUPPRESSED.pop(k, None)
    return len(_SUPPRESSED)
