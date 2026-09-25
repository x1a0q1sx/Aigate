"""OAuth 连接额度/余额查询（端口自 9router open-sse/services/usage/*）。

各家上游的 quota 端点与响应形态互不相同，统一归一化为：
  {
    "plan": str|None,
    "quotas": {名称: {"used","total","unit"?,"reset_at"?,"unlimited","recurring"?,"remaining_pct"?,"display_name"?}},
    "message": str|None,          # 无法解析/未实现时的说明
    "extra": {...}                # provider 特有标量（plan_type、is_quota_exceeded、email 等）
  }

带进程内缓存（默认 300s）与并发合流；Claude 的 usage 端点会 429，
按 token 做冷却，冷却期内回退缓存旧值。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

USAGE_TTL_SECONDS = 300
HTTP_TIMEOUT = 15.0
# Anthropic oauth usage 端点限流后的冷却（9router: 180s）
CLAUDE_COOLDOWN_SECONDS = 180


# ── 归一化小工具（对齐 9router usage/shared.js）──────────────────

def _num(value, fallback=0.0):
    try:
        n = float(value)
        return n if n == n else fallback
    except (TypeError, ValueError):
        return fallback


def _parse_reset(value) -> Optional[str]:
    """多形态时间戳 → ISO 字符串（数字秒/毫秒、RFC3339、Date 串）。"""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            sec = value / 1000 if value > 1e12 else value
            return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat()
        s = str(value).strip()
        if s.isdigit():
            n = int(s)
            sec = n / 1000 if n > 1e12 else n
            return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, OSError):
        return None


def _pct_quota(used_pct, reset_at=None, name_hint=""):
    used = max(0.0, min(100.0, _num(used_pct)))
    return {
        "used": used, "total": 100.0, "remaining": max(0.0, 100.0 - used),
        "reset_at": _parse_reset(reset_at), "unlimited": False,
    }


# ── 额度排序：按到期先后 ─────────────────────────────────────
# 同一账号下常有多个额度包（CodeBuddy 的续包 + 多个赠包、Qoder 的 user/org、
# u1s1 的永久余额 + 今日免费），上游返回顺序没有语义。统一按「最早到期的排最前」
# 展示——先消耗/先过期的包最需要关注。
#
# 排序键（升序）：有 reset_at 的按时间先后在前；无 reset_at（不限量、永久余额、
# 上游没给到期时间）的排最后，同类按名称稳定排序。

_SENTINEL_TS = "9999-12-31"      # 上游表示「无限期/不重置」的哨兵值，等同无到期


def _reset_key(q: dict):
    """额度 → 排序键 (是否无到期, 到期时间戳, 名称)。无到期排最后。"""
    raw = (q or {}).get("reset_at")
    if not raw:
        return (1, 0.0)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = dt.timestamp()
    except (ValueError, TypeError, OSError):
        return (1, 0.0)
    # 9999-12-31 这类「无限期」哨兵值按无到期处理（模型页/连接页都不展示它）
    if ts >= datetime(9000, 1, 1, tzinfo=timezone.utc).timestamp():
        return (1, 0.0)
    return (0, ts)


def sort_quotas(quotas: dict) -> dict:
    """按到期先后重排额度字典（保持 dict 形态，Python 3.7+ 保序）。

    无到期时间的额度（不限量 / 永久余额 / 上游未给）一律排在末尾。
    """
    if not isinstance(quotas, dict) or len(quotas) <= 1:
        return quotas or {}
    items = list(quotas.items())

    def key(item):
        name, q = item
        no_expiry, ts = _reset_key(q)
        return (no_expiry, ts, str(name))

    try:
        items.sort(key=key)
    except Exception:      # 排序永不应让额度查询失败
        return quotas
    return dict(items)


async def _get_json(url: str, headers: dict, params: dict = None):
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(url, headers=headers, params=params)
    return r


async def _post_json(url: str, headers: dict, json_body, content=None):
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        if content is not None:
            r = await client.post(url, headers=headers, content=content)
        else:
            r = await client.post(url, headers=headers, json=json_body if json_body is not None else {})
    return r


# ── 各家 handler ─────────────────────────────────────────────

async def _claude_code_usage(token: str) -> dict:
    """Anthropic oauth usage：five_hour/seven_day/seven_day_X/limits[]."""
    r = await _get_json(
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
        },
    )
    if r.status_code == 429:
        raise _RateLimited("claude usage endpoint 429")
    if not r.is_success:
        return {"plan": "Claude Code", "quotas": {},
                "message": f"Usage API 返回 {r.status_code}（chat 不受影响）"}
    data = r.json()
    quotas = {}

    def window_q(win):
        if isinstance(win, dict) and isinstance(win.get("utilization"), (int, float)):
            return _pct_quota(win["utilization"], win.get("resets_at"))
        return None

    q = window_q(data.get("five_hour"))
    if q:
        quotas["session (5h)"] = q
    q = window_q(data.get("seven_day"))
    if q:
        quotas["weekly (7d)"] = q
    for key, value in data.items():
        if key.startswith("seven_day_") and key != "seven_day":
            q = window_q(value)
            if q:
                quotas[f"weekly {key[len('seven_day_'):]} (7d)"] = q
    for limit in data.get("limits") or []:
        try:
            if limit.get("kind") != "weekly_scoped":
                continue
            model_name = str(
                ((limit.get("scope") or {}).get("model") or {}).get("display_name") or ""
            ).strip().lower()
            if not model_name or not isinstance(limit.get("percent"), (int, float)):
                continue
            quotas[f"weekly {model_name} (7d)"] = _pct_quota(limit["percent"], limit.get("resets_at"))
        except AttributeError:
            continue
    return {"plan": "Claude Code", "quotas": quotas,
            "extra": {"extra_usage": data.get("extra_usage")}}


async def _codex_usage(token: str) -> dict:
    r = await _get_json(
        "https://chatgpt.com/backend-api/wham/usage",
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if not r.is_success:
        return {"quotas": {}, "message": f"Codex Usage API 暂不可用（{r.status_code}）"}
    data = r.json() if r.content else {}
    normal = data.get("rate_limit") or data.get("rate_limits") or \
        (data.get("rate_limits_by_limit_id") or {}).get("codex") or {}
    review = data.get("code_review_rate_limit") or data.get("review_rate_limit") or \
        (data.get("rate_limits_by_limit_id") or {}).get("code_review") or {}
    if not review:
        for entry in data.get("additional_rate_limits") or []:
            ident = str((entry or {}).get("limit_name") or (entry or {}).get("id") or "").lower()
            if "review" in ident:
                review = entry
                break
    spark = data.get("spark_rate_limit") or \
        (data.get("rate_limits_by_limit_id") or {}).get("gpt-5.3-codex-spark") or {}
    if not spark:
        for entry in data.get("additional_rate_limits") or []:
            ident = str((entry or {}).get("limit_name") or (entry or {}).get("id") or "").lower()
            if "spark" in ident:
                spark = entry
                break

    quotas = {}

    def windows(snapshot, prefix=""):
        if not isinstance(snapshot, dict):
            return
        body = snapshot.get("rate_limit") if isinstance(snapshot.get("rate_limit"), dict) else snapshot
        primary = body.get("primary_window") or body.get("primary")
        secondary = body.get("secondary_window") or body.get("secondary")
        if primary:
            quotas[f"{prefix + '_' if prefix else ''}session"] = _pct_quota(
                primary.get("used_percent") or primary.get("percent_used"),
                primary.get("reset_at") or primary.get("resets_at"))
        if secondary:
            quotas[f"{prefix + '_' if prefix else ''}weekly"] = _pct_quota(
                secondary.get("used_percent") or secondary.get("percent_used"),
                secondary.get("reset_at") or secondary.get("resets_at"))

    windows(normal)
    windows(review, "review")
    windows(spark, "spark")
    credits = _num((data.get("rate_limit_reset_credits") or {}).get("available_count"))
    return {
        "plan": data.get("plan_type") or (data.get("summary") or {}).get("plan") or "unknown",
        "quotas": quotas,
        "extra": {
            "limit_reached": bool((normal or {}).get("limit_reached")),
            "review_limit_reached": bool((review or {}).get("limit_reached")),
            "spark_limit_reached": bool((spark or {}).get("limit_reached")),
            "reset_credits_available": int(credits),
        },
    }


async def _github_usage(token: str) -> dict:
    r = await _get_json(
        "https://api.github.com/copilot_internal/user",
        {
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "X-GitHub-Api-Version": "2025-04-01",
            "User-Agent": "GitHubCopilotChat/0.38.0",
            "Editor-Version": "vscode/1.100.0",
            "Editor-Plugin-Version": "copilot-chat/0.26.7",
        },
    )
    if not r.is_success:
        return {"quotas": {}, "message": f"GitHub 额度接口返回 {r.status_code}"}
    data = r.json()

    def snapshot_q(snap, reset_at):
        if not snap:
            return {"used": 0, "total": 0, "unlimited": True}
        entitlement = _num(snap.get("entitlement"))
        remaining = _num(snap.get("remaining"))
        return {
            "used": max(0.0, entitlement - remaining), "total": entitlement,
            "remaining": remaining, "unlimited": bool(snap.get("unlimited")),
            "reset_at": _parse_reset(reset_at),
        }

    quotas = {}
    if isinstance(data.get("quota_snapshots"), dict):
        snaps = data["quota_snapshots"]
        reset_at = data.get("quota_reset_date")
        for name in ("chat", "completions", "premium_interactions"):
            quotas[name] = snapshot_q(snaps.get(name), reset_at)
    elif data.get("monthly_quotas") or data.get("limited_user_quotas"):
        monthly = data.get("monthly_quotas") or {}
        used_q = data.get("limited_user_quotas") or {}
        reset_at = _parse_reset(data.get("limited_user_reset_date"))
        for name in ("chat", "completions"):
            total = _num(monthly.get(name))
            used = _num(used_q.get(name))
            quotas[name] = {"used": used, "total": total, "remaining": max(0.0, total - used),
                            "unlimited": False, "reset_at": reset_at}
    else:
        return {"quotas": {}, "message": "GitHub Copilot 已连接，但未能解析额度数据"}
    return {"plan": data.get("copilot_plan") or data.get("access_type_sku"), "quotas": quotas}


async def _antigravity_usage(token: str) -> dict:
    """loadCodeAssist（项目/套餐）→ fetchAvailableModels（按模型配额分数）
    → retrieveUserQuotaSummary（周额度，尽力而为）。"""
    ide_base = "https://daily-cloudcode-pa.googleapis.com"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/ide/2.11.0 darwin/arm64",
        "X-Client-Name": "antigravity",
        "X-Client-Version": "2.11.0",
    }
    project_id, plan = None, None
    try:
        sub = await _post_json(f"{ide_base}/v1internal:loadCodeAssist",
                               {k: v for k, v in headers.items() if k != "X-Client-Name"},
                               {"metadata": {"ideversion": "antigravity/ide/2.11.0"}, "mode": 1})
        if sub.is_success:
            sd = sub.json()
            project_id = sd.get("cloudaicompanionProject")
            plan = (sd.get("paidTier") or {}).get("id") or (sd.get("currentTier") or {}).get("id") or "free-tier"
    except httpx.HTTPError:
        pass

    quotas = {}
    body = {"project": project_id} if project_id else {}
    models_r = await _post_json(f"{ide_base}/v1internal:fetchAvailableModels", headers, body)
    is_free = not plan or "free" in str(plan)
    if models_r.is_success:
        for model_key, info in (models_r.json().get("models") or {}).items():
            try:
                qi = (info or {}).get("quotaInfo") or {}
                if not qi or (info or {}).get("isInternal"):
                    continue
                frac = _num(qi.get("remainingFraction"), 0.0)
                total = 1000.0
                remaining = round(total * frac)
                quotas[model_key] = {
                    "used": total - remaining, "total": total, "remaining": remaining,
                    "remaining_pct": frac * 100,
                    "reset_at": _parse_reset(qi.get("resetTime")),
                    "unlimited": False,
                    "display_name": (info or {}).get("displayName"),
                    "hidden_on_free": is_free,  # 免费层此数据不可信，仅周额度有意义
                }
            except AttributeError:
                continue

    try:
        weekly_r = await _post_json(f"{ide_base}/v1internal:retrieveUserQuotaSummary", headers, body)
        if weekly_r.is_success:
            wd = weekly_r.json()
            groups = wd.get("groups") or (wd.get("quotaSummary") or {}).get("groups") or []
            for group in groups:
                display = str((group or {}).get("displayName") or "")
                low = display.lower()
                if "gemini" in low:
                    fam = "gemini weekly"
                elif "claude" in low or "gpt" in low or "openai" in low:
                    fam = "claude/gpt weekly"
                else:
                    continue
                for bucket in (group or {}).get("buckets") or []:
                    btext = f"{(bucket or {}).get('bucketId', '')} {(bucket or {}).get('displayName', '')}".lower()
                    if "weekly" not in btext or (bucket or {}).get("disabled"):
                        continue
                    frac = _num((bucket or {}).get("remainingFraction"), 1.0)
                    total = 1000.0
                    remaining = round(total * frac)
                    quotas[fam] = {
                        "used": max(0.0, total - remaining), "total": total, "remaining": remaining,
                        "remaining_pct": frac * 100,
                        "reset_at": _parse_reset((bucket or {}).get("resetTime")),
                        "unlimited": False, "recurring": True,
                    }
                    break
    except httpx.HTTPError:
        pass

    return {"plan": plan, "quotas": quotas,
            "message": None if quotas else "Antigravity 已连接，未取到额度数据（可能需重连获取新 scope）"}


async def _codebuddy_usage(token: str, base_url: str) -> dict:
    """CodeBuddy CN/国际服 billing：data.Response.Data.Accounts 双类型 credit 包。

    续包（refill）：CycleEndTime << DeductionEndTime，用 *Cycle* 数值 + 月/周/日周期标签；
    赠包（bonus）：单周期一次性，用 Capacity 数值。
    """
    from server.core.provider_quirks import quirks_for
    q = quirks_for(base_url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        **dict((q.default_headers if q else None) or {}),
    }
    billing_url = base_url.rsplit("/v2/", 1)[0] + "/v2/billing/meter/get-user-resource"
    r = await _post_json(billing_url, headers, None, content=b"{}")
    if r.status_code in (401, 403):
        return {"quotas": {}, "message": "CodeBuddy 凭证无效或已过期"}
    if not r.is_success:
        return {"quotas": {}, "message": f"CodeBuddy 额度接口错误（{r.status_code}）"}
    try:
        j = r.json()
    except ValueError:
        return {"quotas": {}, "message": "CodeBuddy 额度响应不是 JSON"}
    if _num(j.get("code"), -1) != 0:
        return {"quotas": {}, "message": f"CodeBuddy 额度错误：{j.get('msg') or 'unknown'}"}
    accounts = ((j.get("data") or {}).get("Response") or {}).get("Data") or {}
    accounts = accounts.get("Accounts") if isinstance(accounts, dict) else None
    accounts = accounts if isinstance(accounts, list) else []
    if not accounts:
        return {"quotas": {}, "message": "CodeBuddy 已连接，未发现额度包"}

    def _cycle_end(acc):
        t = _parse_reset(acc.get("CycleEndTime"))
        return t

    def _is_refill(acc):
        ce = acc.get("CycleEndTime")
        de = acc.get("DeductionEndTime")
        try:
            ce_n = float(ce if isinstance(ce, (int, float)) or (isinstance(ce, str) and ce.isdigit()) else 0)
            de_n = float(de if isinstance(de, (int, float)) or (isinstance(de, str) and de.isdigit()) else 0)
            if not (ce_n and de_n):
                return False
            de_ms = de_n if de_n > 1e12 else de_n * 1000
            ce_ms = ce_n if ce_n > 1e12 else ce_n * 1000
            return de_ms - ce_ms > 2 * 86400 * 1000
        except (TypeError, ValueError):
            return False

    def _cadence(acc):
        try:
            st = float(acc.get("CycleStartTime") or 0)
            en = float(acc.get("CycleEndTime") or 0)
            st_ms = st if st > 1e12 else st * 1000
            en_ms = en if en > 1e12 else en * 1000
            days = (en_ms - st_ms) / 86400000
            if days <= 1.5:
                return "Daily"
            if days <= 10:
                return "Weekly"
        except (TypeError, ValueError):
            pass
        return "Monthly"

    refills = [a for a in accounts if _is_refill(a)]
    bonuses = [a for a in accounts if not _is_refill(a)]
    quotas = {}
    seen = {}
    for acc in refills:
        base = _cadence(acc)
        seen[base] = seen.get(base, 0) + 1
        name = base if seen[base] == 1 else f"{base} {seen[base]}"
        quotas[name] = {
            "used": _num(acc.get("CycleCapacityUsedPrecise", acc.get("CycleCapacityUsed"))),
            "total": _num(acc.get("CycleCapacitySizePrecise", acc.get("CycleCapacitySize"))),
            "reset_at": _cycle_end(acc), "unlimited": False, "recurring": True,
        }
    # 赠包按**到期先后**编号：出口会统一按到期排序，若编号沿用上游数组顺序，
    # 排完就会出现 2,3,…,10,1,11… 这种看着像漏号的错觉（实测 33 个赠包时很显眼）。
    bonuses.sort(key=lambda a: (_cycle_end(a) or "9999"))
    for i, acc in enumerate(bonuses):
        quotas[f"Bonus Pack {i + 1}"] = {
            "used": _num(acc.get("CapacityUsedPrecise", acc.get("CapacityUsed"))),
            "total": _num(acc.get("CapacitySizePrecise", acc.get("CapacitySize"))),
            "reset_at": _cycle_end(acc), "unlimited": False, "recurring": False,
        }
    plan = (refills or accounts)[0].get("PackageName") or "CodeBuddy"
    return {"plan": plan, "quotas": quotas}


def _qoder_sane_reset(iso: Optional[str]) -> Optional[str]:
    """吞掉「无到期」哨兵值（9999-12-31）：原样透出会让前端渲染「到期 287 万天后」。"""
    if not iso:
        return None
    try:
        if datetime.fromisoformat(iso).year >= 9000:
            return None
    except ValueError:
        pass
    return iso


def _qoder_parse_sash(body: dict) -> dict:
    """解析 /sash/api/v2/me/usage 响应（Jet-Hub 抓包实测协议，2026-09）。

    余额不只在 userQuota：**签到所得积分落在 addOnQuota（资源包）**——实测
    账号 userQuota.remaining=0 而 addOnQuota.remaining=100；旧端点
    /api/v2/quota/usage 根本不下发这一层，只读 userQuota 会显示 0。
    """
    u = body.get("qoderUsage")
    if not isinstance(u, dict):
        u = {}
    reset_at = _qoder_sane_reset(_parse_reset(u.get("expiresAt") or body.get("expiresAt")))
    quotas: dict = {}

    def _pkg(name: str, q, reset: Optional[str] = None) -> None:
        if not isinstance(q, dict) or not q:
            return
        quotas[name] = {
            "used": _num(q.get("used")), "total": _num(q.get("total")),
            "remaining": _num(q.get("remaining")), "unit": q.get("unit") or "credits",
            "reset_at": _qoder_sane_reset(_parse_reset(q.get("expiresAt"))) or reset,
            "unlimited": False, "recurring": False,
        }

    _pkg("套餐额度", u.get("userQuota"), reset_at)
    _pkg("资源包", u.get("addOnQuota"), reset_at)
    for item in (u.get("dedicatedResourcePackages") or []):
        if isinstance(item, dict):
            _pkg(item.get("name") or item.get("id") or "专用资源包", item, reset_at)
    return {"plan": "Qoder", "quotas": quotas,
            "extra": {"total_usage_pct": _num(u.get("totalUsagePercentage")),
                      "is_quota_exceeded": bool(u.get("isQuotaExceeded"))}}


def _qoder_parse_legacy(body: dict) -> dict:
    """旧端点 /api/v2/quota/usage 的解析（2026-09 仍在线，作回退；不下发资源包层）。"""
    uq = body.get("userQuota") or {}
    oq = body.get("orgResourcePackage") or {}
    reset_at = _qoder_sane_reset(_parse_reset(body.get("expiresAt")))
    quotas: dict = {}
    if uq:
        quotas["套餐额度"] = {
            "used": _num(uq.get("used")), "total": _num(uq.get("total")),
            "remaining": _num(uq.get("remaining")), "unit": uq.get("unit") or "credits",
            "reset_at": reset_at, "unlimited": False,
        }
    if oq:
        quotas["组织资源包"] = {
            "used": _num(oq.get("used")), "total": _num(oq.get("total")),
            "remaining": _num(oq.get("remaining")), "unit": oq.get("unit") or "credits",
            "reset_at": reset_at, "unlimited": False,
        }
    return {"plan": "Qoder", "quotas": quotas,
            "extra": {"total_usage_pct": _num(body.get("totalUsagePercentage")),
                      "is_quota_exceeded": bool(body.get("isQuotaExceeded"))}}


async def _qoder_usage(token: str) -> dict:
    """Qoder 余额：主走 /sash/api/v2/me/usage（与签到同协议族），旧端点兜底。

    sash 端点只需 4 个头、**不需要 COSY/WASM 签名**（同签到端点，
    2026-09-24 生产实测 200）；旧端点看不到资源包层，只在 sash 不可用时兜底。
    """
    if token.startswith("pt-"):
        ex = await _post_json(
            "https://openapi.qoder.sh/api/v1/jobToken/exchange",
            {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": "qodercli/1.0.0"},
            {"personal_token": token},
        )
        if not ex.is_success:
            return {"quotas": {}, "message": f"Qoder PAT 兑换失败（{ex.status_code}）"}
        token = (ex.json() or {}).get("token") or ""
    if not token:
        return {"quotas": {}, "message": "Qoder 无可用于额度查询的凭证"}
    r = await _get_json(
        "https://openapi.qoder.sh/sash/api/v2/me/usage",
        {"Authorization": f"Bearer {token}", "Accept": "application/json",
         "Cosy-ClientType": "5", "User-Agent": "Qoder"})
    if r.is_success:
        body = r.json() if r.content else {}
        if body.get("displayMode") == "enterprise":
            # 企业版不下发额度数字，只有外部用量链接（报 0 会误导用户以为没额度）
            return {"plan": "Qoder", "quotas": {},
                    "message": "企业版账号不提供额度数字（仅提供外部用量链接）"}
        out = _qoder_parse_sash(body)
        if out["quotas"]:
            return out
        # 形状异常（无任何条目）→ 旧端点再试一次，两路都空才报错
    r2 = await _get_json("https://openapi.qoder.sh/api/v2/quota/usage",
                         {"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if not r2.is_success:
        return {"quotas": {}, "message": f"Qoder 已连接，额度接口返回 {r2.status_code}"}
    out = _qoder_parse_legacy(r2.json() if r2.content else {})
    if out["quotas"]:
        return out
    return {"quotas": {}, "message": "Qoder 额度响应无任何条目（响应形状可能已变）"}


async def _u1s1_usage(token: str) -> dict:
    """GET https://api.u1s1.io/v1/me：永久余额 + 今日免费（USD 与 tokens 双口径）。

    有设备密钥（u1s1 重登录过）时按官方 DPoP 形态请求；否则退回 Bearer api_key。"""
    from server.core.provider_quirks import quirks_for
    q = quirks_for("https://api.u1s1.io/v1")
    url = "https://api.u1s1.io/v1/me"
    headers = {"Authorization": f"Bearer {token}",
               **dict((q.default_headers if q else None) or {})}
    try:
        from server.db import AsyncSessionLocal
        from server.core.oauth_client import get_oauth_client
        async with AsyncSessionLocal() as db:
            material = await get_oauth_client().get_device_signing_material("u1s1", db)
        if material:
            from server.core.dpop import sign_proof
            headers.update(sign_proof(material["priv"], material["bearer"], "GET", url))
    except Exception:
        pass
    r = await _get_json(url, headers)
    if not r.is_success:
        return {"quotas": {}, "message": f"u1s1 额度接口返回 {r.status_code}"}
    me = r.json() if r.content else {}
    tpu = _num(me.get("tokens_per_usd"))
    quotas = {}
    if "remaining_usd" in me:
        bal = _num(me.get("remaining_usd"))
        quotas["永久余额"] = {"used": 0.0, "total": bal, "remaining": bal,
                              "unit": "USD", "unlimited": False,
                              "display_name": f"≈ {int(bal * tpu):,} tokens" if tpu else None}
    daily_total = _num(me.get("daily_free_usd"))
    daily_left = _num(me.get("daily_free_remaining_usd"), daily_total)
    if daily_total > 0:
        quotas["今日免费"] = {
            "used": max(0.0, daily_total - daily_left), "total": daily_total,
            "remaining": daily_left, "unit": "USD", "unlimited": False, "recurring": True,
            "display_name": f"≈ {int(daily_left * tpu):,} tokens" if tpu else None,
        }
    return {"plan": "u1s1", "quotas": quotas,
            "extra": {"email": me.get("email"), "free_claim": me.get("free_claim"),
                      "tokens_per_usd": tpu}}


async def _lobsterai_usage(token: str) -> dict:
    """LobsterAI 积分余额：GET /api/user/profile-summary。

    必须用 profile-summary 而非 /api/user/quota —— 后者只显示 freeCreditsTotal=300，
    **不含活动积分**（实测某账号 profile-summary 有 5297.72，quota 只有 300）。

    身份字段（uuid/first_keyfrom/…）从连接 scope 列读回；缺失时该端点仍可查
    （它只用 Bearer 鉴权），故降级而不报错。
    """
    meta: dict = {}
    try:
        from server.db import AsyncSessionLocal
        from server.core.oauth_client import get_oauth_client
        async with AsyncSessionLocal() as db:
            meta = await get_oauth_client().get_token_meta("lobsterai", db)
    except Exception:
        meta = {}
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "LobsterAI/0.1.0",
    }
    cv = str(meta.get("client_version") or "")
    if cv:
        headers["X-LobsterAI-Client-Version"] = cv
    headers["X-LobsterAI-Client-Capabilities"] = (
        "kimi-k3-agentic-v1,thinking-level-control-v1")
    r = await _get_json("https://lobsterai-server.youdao.com/api/user/profile-summary",
                        headers)
    if not r.is_success:
        return {"quotas": {}, "message": f"LobsterAI 已连接，额度接口返回 {r.status_code}"}
    body = r.json() if r.content else {}
    if not isinstance(body, dict) or body.get("code") not in (0, None):
        return {"quotas": {}, "message": f"LobsterAI 额度响应异常：{str(body)[:120]}"}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    quotas = {}
    total = _num(data.get("totalCreditsRemaining"))
    items = data.get("creditItems")
    if isinstance(items, list):
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            remaining = max(0.0, _num(item.get("creditsRemaining")))
            name = str(item.get("type") or f"积分包 {i + 1}")
            # 服务端只下发剩余量（无总额/已用）→ total=0 且 UI 会显示 '?'
            # （不编造 total，避免把「剩余」伪装成「1:1 进度」）
            quotas[name] = {"used": 0.0, "total": 0.0, "remaining": remaining,
                            "unit": "credit", "unlimited": False, "recurring": False}
    if not quotas and total > 0:
        quotas["积分"] = {"used": 0.0, "total": 0.0, "remaining": max(0.0, total),
                          "unit": "credit", "unlimited": False, "recurring": False}
    if not quotas:
        # total 为 0 且无明细 → 视为「查不到」而非「余额为 0」（字段缺失时
        # _num 返回 0，会把解析失败伪装成 0 积分）
        return {"quotas": {}, "message": "LobsterAI 额度响应无任何条目"}
    return {"plan": "LobsterAI", "quotas": quotas,
            "extra": {"total_credits_remaining": total}}


class _RateLimited(Exception):
    pass

# ── 调度与缓存 ───────────────────────────────────────────────

_CACHE: dict = {}        # (code, token_hash) -> (expires_at_monotonic, result)
_INFLIGHT: dict = {}     # key -> asyncio.Task
_COOLDOWN: dict = {}     # (code, token_hash) -> until_monotonic（429 后）


def _key(provider_code: str, token: str):
    return (provider_code, hashlib.sha1((token or "").encode()).hexdigest()[:16])


async def get_connection_usage(provider_code: str, access_token: str,
                               force: bool = False) -> dict:
    """按 provider code 查询额度。永不抛异常；失败/未实现以 message 表达。"""
    key = _key(provider_code, access_token)
    now = time.monotonic()
    if not force:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return dict(hit[1], cached=True)
        cool = _COOLDOWN.get(key)
        if cool and cool > now and provider_code == "claude_code":
            if hit:  # 冷却期内回退旧值
                return dict(hit[1], cached=True, cooling=True)
            return {"plan": None, "quotas": {}, "message": "Claude 额度接口限流冷却中，稍后再查",
                    "cooling": True}

    existing = _INFLIGHT.get(key)
    if existing and not force:
        return await asyncio.shield(existing)

    async def _run():
        try:
            result = await _dispatch(provider_code, access_token)
        except _RateLimited as e:
            _COOLDOWN[key] = time.monotonic() + CLAUDE_COOLDOWN_SECONDS
            stale = _CACHE.get(key)
            if stale:
                return dict(stale[1], cached=True, cooling=True)
            result = {"plan": None, "quotas": {}, "message": f"额度接口限流：{e}"}
        except httpx.HTTPError as e:
            result = {"plan": None, "quotas": {}, "message": f"额度查询网络错误：{type(e).__name__}"}
        except Exception as e:  # 防御：任何解析异常都不冒穿
            logger.warning("oauth usage error for %s: %s", provider_code, e)
            result = {"plan": None, "quotas": {}, "message": f"额度查询失败：{type(e).__name__}: {e}"}
        # 统一按到期先后排序（在此收口：缓存/合流/各 provider 分支都经这里出去）
        if result.get("quotas"):
            result["quotas"] = sort_quotas(result["quotas"])
        if result.get("quotas"):  # 只缓存有真实数据的结果
            _CACHE[key] = (time.monotonic() + USAGE_TTL_SECONDS, result)
        return dict(result, provider_code=provider_code)

    task = asyncio.get_event_loop().create_task(_run())
    _INFLIGHT[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _INFLIGHT.get(key) is task:
            _INFLIGHT.pop(key, None)


async def _dispatch(provider_code: str, token: str) -> dict:
    if provider_code == "claude_code":
        return await _claude_code_usage(token)
    if provider_code == "codex":
        return await _codex_usage(token)
    if provider_code == "github_copilot":
        return await _github_usage(token)
    if provider_code == "antigravity":
        return await _antigravity_usage(token)
    if provider_code == "codebuddy_cn":
        return await _codebuddy_usage(token, "https://copilot.tencent.com/v2/chat/completions")
    if provider_code == "codebuddy_intl":
        return await _codebuddy_usage(token, "https://www.codebuddy.ai/v2/chat/completions")
    if provider_code == "qoder":
        return await _qoder_usage(token)
    if provider_code == "u1s1":
        return await _u1s1_usage(token)
    if provider_code == "lobsterai":
        return await _lobsterai_usage(token)
    return {"plan": None, "quotas": {},
            "message": "该服务商上游没有公开的额度接口（与 9router 覆盖范围一致）"}
