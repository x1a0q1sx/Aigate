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
    if not r.ok:
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
    if not r.ok:
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
    if not r.ok:
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
        if sub.ok:
            sd = sub.json()
            project_id = sd.get("cloudaicompanionProject")
            plan = (sd.get("paidTier") or {}).get("id") or (sd.get("currentTier") or {}).get("id") or "free-tier"
    except httpx.HTTPError:
        pass

    quotas = {}
    body = {"project": project_id} if project_id else {}
    models_r = await _post_json(f"{ide_base}/v1internal:fetchAvailableModels", headers, body)
    is_free = not plan or "free" in str(plan)
    if models_r.ok:
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
        if weekly_r.ok:
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
    if not r.ok:
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
    for i, acc in enumerate(bonuses):
        quotas[f"Bonus Pack {i + 1}"] = {
            "used": _num(acc.get("CapacityUsedPrecise", acc.get("CapacityUsed"))),
            "total": _num(acc.get("CapacitySizePrecise", acc.get("CapacitySize"))),
            "reset_at": _cycle_end(acc), "unlimited": False, "recurring": False,
        }
    plan = (refills or accounts)[0].get("PackageName") or "CodeBuddy"
    return {"plan": plan, "quotas": quotas}


async def _qoder_usage(token: str) -> dict:
    """openapi.qoder.sh/api/v2/quota/usage；pt- PAT 先换 jt- 任务 token。"""
    if token.startswith("pt-"):
        ex = await _post_json(
            "https://openapi.qoder.sh/api/v1/jobToken/exchange",
            {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": "qodercli/1.0.0"},
            {"personal_token": token},
        )
        if not ex.ok:
            return {"quotas": {}, "message": f"Qoder PAT 兑换失败（{ex.status_code}）"}
        token = (ex.json() or {}).get("token") or ""
    if not token:
        return {"quotas": {}, "message": "Qoder 无可用于额度查询的凭证"}
    r = await _get_json("https://openapi.qoder.sh/api/v2/quota/usage",
                        {"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if not r.ok:
        return {"quotas": {}, "message": f"Qoder 已连接，额度接口返回 {r.status_code}"}
    body = r.json() if r.content else {}
    uq = body.get("userQuota") or {}
    oq = body.get("orgResourcePackage") or {}
    reset_at = _parse_reset(body.get("expiresAt"))
    quotas = {}
    if uq:
        quotas["user"] = {
            "used": _num(uq.get("used")), "total": _num(uq.get("total")),
            "remaining": _num(uq.get("remaining")), "unit": uq.get("unit") or "credits",
            "reset_at": reset_at, "unlimited": False,
        }
    if oq:
        quotas["organization"] = {
            "used": _num(oq.get("used")), "total": _num(oq.get("total")),
            "remaining": _num(oq.get("remaining")), "unit": oq.get("unit") or "credits",
            "reset_at": reset_at, "unlimited": False,
        }
    return {"plan": "Qoder", "quotas": quotas,
            "extra": {"total_usage_pct": _num(body.get("totalUsagePercentage")),
                      "is_quota_exceeded": bool(body.get("isQuotaExceeded"))}}


async def _u1s1_usage(token: str) -> dict:
    """GET https://api.u1s1.io/v1/me：永久余额 + 今日免费（USD 与 tokens 双口径）。"""
    from server.core.provider_quirks import quirks_for
    q = quirks_for("https://api.u1s1.io/v1")
    headers = {"Authorization": f"Bearer {token}",
               **dict((q.default_headers if q else None) or {})}
    r = await _get_json("https://api.u1s1.io/v1/me", headers)
    if not r.ok:
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
    return {"plan": None, "quotas": {},
            "message": "该服务商上游没有公开的额度接口（与 9router 覆盖范围一致）"}
