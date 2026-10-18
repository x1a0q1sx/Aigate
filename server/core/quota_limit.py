"""上游「每日限额」识别与重置时间解析（2026-10-16）。

生产实证（workbuddy / CodeBuddy 国服与国际服，报错文案在 429 报文里）：

- CN（copilot.tencent.com）：
  {"code":6004,"msg":"您的使用量已超出频率限制，将在 2026-10-17 15:26:42 UTC+8 重置，
   您也可以切换其他模型继续使用。"}
- Intl（www.codebuddy.ai）：
  {"code":6004,"msg":"usage exceeds frequency limit, but don't worry, your usage will
   reset at 2026-10-13 09:26:43 UTC+8, alternatively, you can switch to the other
   models to continue"}

关键语义：
  1) 这是 **(账号 × 模型)** 的每日限额（上游明说可切换其他模型），不是账号级封禁；
  2) 文案**自带精确重置时间**——识别到就按该时间点冷却，而不是常规指数退避
     （常规退避 60s→120s… 会在限额期内反复牺牲请求去试探，生产实测同一窗口
     对已限额模型重试了 2293 次）；
  3) 必须与 `14003 too many requests`（瞬时频率限流、无重置时间）区分开——
     后者仍走原有短冷却，不能按「每日限额」处理。

本模块只做纯文本识别/解析（零依赖，可单测）；记录的读写落在
oauth_client.note_request_result 与 core.quota_store 的查询函数里。
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── 识别：限定「每日限额」语义（必须带重置时间或明确 code）──────────────
# code 6004 = 每日用量限额（CN/Intl 同一码）；14003 = 瞬时频率限流（不算）。
_DAILY_LIMIT_CODE_RE = re.compile(r'"code"\s*:\s*6004\b')

# 文案特征（中英双版）。「您也可以切换其他模型」「alternatively, you can switch」
# 是限额专属句式；纯 14003 没有这些词。
_LIMIT_TEXT_MARKERS = (
    "您的使用量已超出频率限制",
    "usage exceeds frequency limit",
)


def is_daily_limit_text(error) -> bool:
    """错误文本是否为上游「每日限额」提示（(账号×模型) 粒度，自带重置时间）。

    命中任一即真：
      - 结构化 code 6004（CN/Intl 同码，最可靠）；
      - 中文文案「您的使用量已超出频率限制」；
      - 英文文案「usage exceeds frequency limit」。
    `14003 too many requests` 不命中（无上述特征）——它是瞬时频率限流。"""
    text = str(error or "")
    if not text:
        return False
    if _DAILY_LIMIT_CODE_RE.search(text):
        return True
    low = text.lower()
    return any(m in text or m in low for m in _LIMIT_TEXT_MARKERS)


# ── 解析：从文案里抽出重置时间点 ────────────────────────────────────────
# 样例："将在 2026-10-17 15:26:42 UTC+8 重置" / "your usage will reset at 2026-10-13 09:26:43 UTC+8"
_DT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
    r"(?:\s*UTC\s*([+-])\s*(\d{1,2}))?"
)
_UTC8_RE = re.compile(r"UTC\s*\+\s*8", re.IGNORECASE)


def parse_daily_limit_reset(error) -> Optional[datetime]:
    """从限额文案里解析重置时间 → naive UTC datetime；解析不出返回 None。

    - 带 `UTC+8` 后缀（实测形态）→ 按 UTC+8 转 UTC；
    - 无时区后缀 → 按 UTC+8 假定（上游 CN/Intl 均以北京时间下发；
      假定错误也只会让冷却略早结束，不会永久锁死——查询侧按 reset_at 过滤）；
    - 时间已过（如补发旧日志）→ 返回 None（不产生过期记录）。"""
    text = str(error or "")
    m = _DT_RE.search(text)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    # 时区偏移：显式 UTC±N 优先，否则查全文的 UTC+8 标记，再否则假定 UTC+8
    off_hours = None
    if m.group(2) and m.group(3):
        off_hours = int(m.group(3)) * (1 if m.group(2) == "+" else -1)
    elif _UTC8_RE.search(text):
        off_hours = 8
    else:
        off_hours = 8
    try:
        # 本地时间(UTC+off) → UTC：减去偏移
        utc_dt = dt - timedelta(hours=off_hours)
    except (OverflowError, ValueError):
        return None
    # 已过期（补日志等）→ 不建记录；留 1 分钟容差防时钟抖动
    if utc_dt <= datetime.utcnow() - timedelta(minutes=1):
        return None
    return utc_dt


def daily_limit_reset_or_default(error, default_seconds: int = 3600) -> datetime:
    """解析重置时间；解析不出时给「现在 + default_seconds」兜底（默认 1 小时）。

    兜底上限也受调用方（note_request_result）的封顶约束。
    余额耗尽（402，无重置时间）由调用方传 default_seconds=1800（30 分钟，
    与 cline2api 的 402 冷却策略一致）——充值后最多半小时自动重试。"""
    got = parse_daily_limit_reset(error)
    if got is not None:
        return got
    return datetime.utcnow() + timedelta(seconds=max(60, int(default_seconds or 3600)))


# 网关内部生成的「全部账号该模型额度用完」错误前缀（credential_resolver）
QUOTA_LIMITED_PREFIX = "quota_limited"

# ── 余额/积分耗尽（无重置时间，按固定短冷却）────────────────────────────
# Cline（cline.bot）按量计费模型在余额不足时返回：
#   HTTP 402 {"error":{"code":"insufficient_credits",
#            "message":"Insufficient balance. Your Cline Credits balance is $0.00",
#            "current_balance":0.00393,...}}
# 这是**账号余额**问题（Cline Pass 订阅模型不受影响，它们走 cline-pass/* 不计 credits），
# 上游不会给出恢复时间 → 按固定冷却（默认 30 分钟，与 cline2api 同策略）记录，
# 避免每个请求都先烧一次 402 往返。
_CREDIT_EXHAUSTED_MARKERS = (
    "insufficient_credits",
    "insufficient balance",
    "insufficient credits",
    "payment required",
)


def is_credit_exhausted_text(error) -> bool:
    """错误文本是否为「余额/积分耗尽」（402 insufficient_credits 等）。

    与每日限额区分：这类错误**没有**重置时间，恢复靠用户充值/换账号；
    记录时用固定冷却（见 oauth_client.note_request_result）。"""
    text = str(error or "")
    if not text:
        return False
    low = text.lower()
    if "402" in low and ("payment required" in low or "insufficient" in low):
        return True
    return any(m in low for m in _CREDIT_EXHAUSTED_MARKERS)


def is_quota_limited_error(error) -> bool:
    """是否为网关生成的 quota_limited 错误（选号全被限额拦截时）。"""
    return str(error or "").startswith(QUOTA_LIMITED_PREFIX)


def is_quota_related_error(error) -> bool:
    """限额相关错误（上游每日限额文案 / 余额耗尽 / 网关 quota_limited 错误）。

    这类错误不是模型故障 → 调用方不应把它计入模型冷却（指数退避罚时）：
    限额记录本身已精确管控到重置时间点，账号切换与到期恢复自动生效。"""
    return (is_daily_limit_text(error) or is_credit_exhausted_text(error)
            or is_quota_limited_error(error))
