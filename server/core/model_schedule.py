"""模型生效时段（定时免费/限时模型）——纯函数，零 DB 依赖。

背景（2026-10-17 用户需求）：上游存在「按时段免费/打折」的模型，例如
CodeBuddy Hy4 preview 每晚 23:00-08:00 不计积分（活动 2026-09-11~10-31）、
Qoder ultimate/Qwen3.8-Max/Qwen3.7-Plus 错峰 22:00-08:00 打折（目录自带
promotion 字段）。这类模型只在特定时段划算，其余时段应跳过（避免误花积分）
或降权（急用时仍可命中）。

存储结构（models.active_schedule JSON）::

    {
      "mode": "only" | "boost",       # only=仅时段内可用；boost=时段内优先
      "boost": 30,                    # boost 模式时段内的 final_score 加分
      "timezone": "Asia/Shanghai",    # IANA 时区；缺省 Asia/Shanghai
      "rules": [
        {"start": "23:00", "end": "08:00",     # 支持跨零点（start > end）
         "days": [1,2,3,4,5,6,7],              # ISO 星期（1=周一）；null/空=每天
         "date_from": "2026-09-11",            # 可选，含当天
         "date_to": "2026-10-31",              # 可选，含当天
         "label": "夜间免费"}
      ]
    }

语义约定：
- 跨零点窗口的「归属日」= 窗口开始那天。例如周二 02:00 属于周一 23:00 开的窗口，
  days 过滤按周一算（用户直觉：周一的夜班）。
- date_from/date_to 按归属日比较，均含端点。
- start == end 视为全天。
- 未配置 / rules 为空 / 解析失败 → 不拦截、不加分（安全默认：绝不因配置错误饿死模型）。
"""
from __future__ import annotations

import re
from datetime import datetime, date, time, timedelta, timezone as _tz
from typing import Any, Dict, List, Optional, Tuple

try:  # Windows 无 tzdata 时退化为固定偏移（Asia/Shanghai = UTC+8）
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_DEFAULT_TZ = "Asia/Shanghai"
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# 无 tzdata 时的兜底偏移（只覆盖常见中国区；其它时区退化为 UTC）
_FALLBACK_OFFSETS = {"Asia/Shanghai": 8, "Asia/Singapore": 8, "Asia/Hong_Kong": 8,
                     "Asia/Tokyo": 9, "UTC": 0, "Etc/UTC": 0}


def _tzinfo(name: str):
    name = (name or _DEFAULT_TZ).strip() or _DEFAULT_TZ
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    hours = _FALLBACK_OFFSETS.get(name)
    if hours is None:
        return _tz.utc
    return _tz(timedelta(hours=hours))


def _parse_hhmm(v: Any) -> Optional[time]:
    if not isinstance(v, str):
        return None
    m = _HHMM_RE.match(v.strip())
    if not m:
        return None
    return time(int(m.group(1)), int(m.group(2)))


def _parse_date(v: Any) -> Optional[date]:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return date.fromisoformat(v.strip()[:10])
    except ValueError:
        return None


def _rule_active(rule: dict, local_now: datetime) -> bool:
    start = _parse_hhmm(rule.get("start"))
    end = _parse_hhmm(rule.get("end"))
    if start is None or end is None:
        return False
    t = local_now.time()
    # 归属日：跨零点且当前在凌晨段 → 归属前一天
    if start < end:
        inside = start <= t < end
        anchor = local_now.date()
    elif start > end:
        inside = t >= start or t < end
        anchor = local_now.date() if t >= start else (local_now.date() - timedelta(days=1))
    else:
        inside = True
        anchor = local_now.date()
    if not inside:
        return False
    days = rule.get("days")
    if isinstance(days, list) and days:
        try:
            iso_days = {int(d) for d in days}
        except (TypeError, ValueError):
            iso_days = set()
        if iso_days and anchor.isoweekday() not in iso_days:
            return False
    df = _parse_date(rule.get("date_from"))
    dt = _parse_date(rule.get("date_to"))
    if df and anchor < df:
        return False
    if dt and anchor > dt:
        return False
    return True


def _next_window_start(rule: dict, local_now: datetime) -> Optional[datetime]:
    """从 now 起向后找 14 天内该规则的下一次开始时刻（用于报错文案）。"""
    start = _parse_hhmm(rule.get("start"))
    end = _parse_hhmm(rule.get("end"))
    if start is None or end is None:
        return None
    days = rule.get("days")
    iso_days: Optional[set] = None
    if isinstance(days, list) and days:
        try:
            iso_days = {int(d) for d in days}
        except (TypeError, ValueError):
            iso_days = None
    df = _parse_date(rule.get("date_from"))
    dt = _parse_date(rule.get("date_to"))
    for i in range(0, 15):
        d = local_now.date() + timedelta(days=i)
        if iso_days and d.isoweekday() not in iso_days:
            continue
        if df and d < df:
            continue
        if dt and d > dt:
            continue
        cand = datetime.combine(d, start, tzinfo=local_now.tzinfo)
        if cand > local_now:
            return cand
    return None


def normalize_schedule(raw: Any) -> Optional[Dict[str, Any]]:
    """校验并规范化前端提交的时段配置；无法识别返回 None（=清空）。

    只做结构收敛，不猜测用户意图：规则缺 start/end 直接丢弃；全部丢弃则返回 None。
    """
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode") or "only").lower()
    if mode not in ("only", "boost"):
        mode = "only"
    try:
        boost = int(raw.get("boost") or 30)
    except (TypeError, ValueError):
        boost = 30
    boost = max(0, min(100, boost))
    tz_name = str(raw.get("timezone") or _DEFAULT_TZ).strip() or _DEFAULT_TZ
    rules_in = raw.get("rules")
    if not isinstance(rules_in, list):
        rules_in = []
    rules: List[dict] = []
    for r in rules_in[:12]:
        if not isinstance(r, dict):
            continue
        s = _parse_hhmm(r.get("start"))
        e = _parse_hhmm(r.get("end"))
        if s is None or e is None:
            continue
        days = r.get("days")
        if isinstance(days, list) and days:
            try:
                days = sorted({int(d) for d in days if 1 <= int(d) <= 7})
            except (TypeError, ValueError):
                days = []
        else:
            days = []
        rule = {
            "start": s.strftime("%H:%M"),
            "end": e.strftime("%H:%M"),
            "days": days,
            "date_from": (r.get("date_from") or "").strip()[:10],
            "date_to": (r.get("date_to") or "").strip()[:10],
            "label": str(r.get("label") or "").strip()[:60],
        }
        rules.append(rule)
    if not rules:
        return None
    return {"mode": mode, "boost": boost, "timezone": tz_name, "rules": rules}


def evaluate(schedule: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """评估模型当前时段状态。

    返回::

        {"configured": bool, "active": bool, "mode": "only"|"boost",
         "blocked": bool, "boost": int, "label": str,
         "next_open": iso str | None, "reason": str}

    blocked=True 表示「仅时段内可用」且当前不在任何窗口内 —— 调用方应跳过该模型。
    boost>0 表示「时段内优先」且当前在窗口内 —— 调用方应加分。
    """
    empty = {"configured": False, "active": False, "mode": "only", "blocked": False,
             "boost": 0, "label": "", "next_open": None, "reason": ""}
    if not isinstance(schedule, dict):
        return empty
    rules = schedule.get("rules")
    if not isinstance(rules, list) or not rules:
        return empty
    tzinfo = _tzinfo(str(schedule.get("timezone") or _DEFAULT_TZ))
    local_now = (now or datetime.now(_tz.utc)).astimezone(tzinfo)
    mode = str(schedule.get("mode") or "only").lower()
    if mode not in ("only", "boost"):
        mode = "only"
    try:
        boost_val = max(0, min(100, int(schedule.get("boost") or 30)))
    except (TypeError, ValueError):
        boost_val = 30
    matched = None
    for r in rules:
        if isinstance(r, dict) and _rule_active(r, local_now):
            matched = r
            break
    active = matched is not None
    label = ""
    if matched:
        label = str(matched.get("label") or "")
    else:
        for r in rules:
            if isinstance(r, dict) and r.get("label"):
                label = str(r.get("label") or "")
                break
    blocked = (mode == "only") and not active
    next_open = None
    if blocked:
        candidates = []
        for r in rules:
            if not isinstance(r, dict):
                continue
            nxt = _next_window_start(r, local_now)
            if nxt is not None:
                candidates.append(nxt)
        if candidates:
            next_open = min(candidates).isoformat()
    reason = ""
    if blocked:
        reason = f"outside active window（{label or '时段外'}，下次开放 {next_open or '未知'}）"
    elif active and mode == "boost":
        reason = f"in active window（{label or '时段内'} +{boost_val}）"
    return {
        "configured": True,
        "active": active,
        "mode": mode,
        "blocked": blocked,
        "boost": boost_val if (active and mode == "boost") else 0,
        "label": label,
        "next_open": next_open,
        "reason": reason,
    }


def evaluate_model(model: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """ORM Model → evaluate(model.active_schedule)。属性缺失/为 None 时安全返回未配置。"""
    try:
        schedule = getattr(model, "active_schedule", None)
    except Exception:
        schedule = None
    return evaluate(schedule, now=now)


def schedule_summary(schedule: Any) -> str:
    """人类可读摘要（前端徽标 tooltip / 日志用）。"""
    if not isinstance(schedule, dict) or not schedule.get("rules"):
        return ""
    parts = []
    for r in schedule.get("rules") or []:
        if not isinstance(r, dict):
            continue
        seg = f"{r.get('start')}-{r.get('end')}"
        if r.get("date_from") or r.get("date_to"):
            seg += f"（{r.get('date_from') or '…'}~{r.get('date_to') or '…'}）"
        if r.get("label"):
            seg = f"{r.get('label')}: {seg}"
        parts.append(seg)
    mode = "仅时段内" if str(schedule.get("mode") or "only") == "only" else f"时段内优先 +{schedule.get('boost') or 30}"
    return f"{mode}｜" + "；".join(parts)
