"""每日签到（领取上游免费积分/额度）。

协议来源：Jet-Hub（github.com/zhengwuji/Jet-Hub，MIT）源码逐行核对 + 本机生产实测
（2026-09-24）。三个平台的实测结论：

  codebuddy_cn   HTTP 200 ✅  active=True  today_checked_in=True  streak_days=6
                 daily_credit=100  total_credits=600  activity=高校新生攻略
  codebuddy_intl HTTP 200 ✅  端点存在，实测 active=False（本期活动未开启）
  qoder          HTTP 200 ✅  showCampaign=False campaigns=[]（今日已领语义）
  u1s1           ❌ 8 个候选端点全 404 —— 无签到接口；其「登录打卡赠送」是
                 **登录时上游自动发放**（packages[].kind="login_checkin"），
                 不需要也不能由客户端触发。故能力矩阵登记为 False。

设计要点（均为上游协议的硬约束，改动前先读这里）：
- **严格串行**：Jet-Hub 三处注释强调「并发易触发风控」，并有单测锁死
  maxInFlight==1。本模块 likewise 顺序执行、单账号失败不中断整批、不做重试退避。
- **「今天已领」不是失败**：CodeBuddy 靠 body code 10001/1001，Qoder 靠
  body.replayed（HTTP 仍是 200 且无 benefit）——只看 HTTP 状态会把「已领」
  误报成「+100 积分」。
- **签到全程在推理请求路径之外**：任何失败只落 checkin_logs，绝不触碰路由。
- **幂等靠 ATTEMPTED_KINDS（含 inactive）**：自动触发只在计划时刻之后发生，
  那时上游活动早已刷新完，仍报「未开启」就是当天真没活动，反复重试无意义。
  ⚠️ 2026-09-24 生产教训：只把 claimed/already_claimed 当完成，会让「活动未开启」
  的站在进程重启时每次补签都打一遍上游（一次崩溃循环刷出 25 条重复日志）。
  failed 仍不在此列——失败必须允许下次重试。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0
# 北京时间：上游活动按 UTC+8 刷新（Qoder 每日 10:00），显式时区不依赖服务器 TZ
CST = timezone(timedelta(hours=8))

# 能力真相源：True=支持签到；False=上游无接口；None=待探测（UI 灰态不渲染按钮）
CHECKIN_CAPABILITIES: Dict[str, Optional[bool]] = {
    "codebuddy_cn": True,
    "codebuddy_intl": True,     # 端点实测存在（HTTP 200）
    "qoder": True,
    "lobsterai": True,          # 三步协议（slot → context → check_in），+100 积分/天
    "codearts": True,           # 四步协议（账户类型 → delivery → claim → confirm）
    "trae": True,               # 两步：status 预检 → claim → 补查 status，+150 credits/天
    "u1s1": False,              # 实测无签到接口（登录即自动发放 login_checkin 包）
}

# 视为「今日已完成」的 kind —— 定时任务据此幂等跳过
DONE_KINDS = ("claimed", "already_claimed")
# 视为「今日已尝试、不必再自动重试」的 kind。
# inactive（活动未开启）也算：自动触发只在**计划时刻之后**发生（定时 10:30 /
# 启动补签仅在已过点时才跑），而那一刻上游活动早已刷新完毕 —— 此时仍报「未开启」
# 就是当天真的没有活动，反复重试无意义。
# ⚠️ 2026-09-24 生产教训：此前只把 claimed/already_claimed 当完成，导致
# codebuddy_intl（活动未开启）在进程反复重启时每次补签都打一遍上游，
# 一次崩溃循环刷出 25 条重复日志。failed 仍不在此列——失败必须允许重试。
ATTEMPTED_KINDS = DONE_KINDS + ("inactive",)

# CodeBuddy 业务码（实测 + 静态分析）
CB_ALREADY = (10001, 1001)
CB_NO_QUALIFICATION = 1002
CB_ACTIVITY_ENDED = 1003

# CodeBuddy 产品配置：X-Domain / User-Agent 必须与请求的 base_url 一致，
# 不能跟着凭据里可能过期的 domain 走（Jet-Hub 记录的踩坑）
_CODEBUDDY_PRODUCTS = {
    "codebuddy_cn": {"host": "copilot.tencent.com", "base": "https://copilot.tencent.com"},
    "codebuddy_intl": {"host": "www.codebuddy.ai", "base": "https://www.codebuddy.ai"},
}


@dataclass
class ClaimOutcome:
    """单账号签到结果（端口自 Jet-Hub 的 ClaimOutcome）。"""
    kind: str                       # claimed | already_claimed | inactive | unsupported | failed
    credit: float = 0.0
    streak_days: int = 0
    total_credits: float = 0.0
    activity_name: str = ""
    message: str = ""
    upstream_code: Optional[int] = None
    error: str = ""
    # 供 UI 展示的原始状态（如 CodeBuddy 的 checkin_dates）
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否算「今天已完成」——已领与刚领都算成功。"""
        return self.kind in DONE_KINDS


# ── 小工具 ───────────────────────────────────────────────────

def _num(v, fallback: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else fallback
    except (TypeError, ValueError):
        return fallback


def _read_bool(d: dict, key: str) -> bool:
    return d.get(key) is True


def _read_int(d: dict, key: str) -> int:
    return int(_num(d.get(key)))


async def _request(client: httpx.AsyncClient, method: str, url: str,
                   headers: dict, body: Optional[bytes] = None):
    """发请求并返回 (status, parsed_or_None, raw_text)。

    401/403 时上游可能返回 HTML 而非 JSON（CodeBuddy 实测），因此统一先取 text
    再尝试 parse —— 直接 .json() 会抛异常并丢失「凭据失效」这个关键判据。
    """
    if method == "GET":
        r = await client.get(url, headers=headers)
    else:
        r = await client.post(url, headers=headers, content=body if body is not None else b"{}")
    text = r.text
    try:
        parsed = r.json()
    except Exception:
        parsed = None
    return r.status_code, parsed, text


# ── CodeBuddy（CN / International）────────────────────────────

def _codebuddy_headers(token: str, product: dict) -> dict:
    """签到/状态请求头。不含 X-Device-Token（Jet-Hub 实测非必需）。

    X-User-Id / X-Enterprise-Id 同为可选头（AIGate 的 CodeBuddy 登录只存
    access_token，未存 user_id）→ 不发。
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Domain": product["host"],
        "X-Product": "SaaS",
        "X-Product-Code": "codebuddy",
        "User-Agent": "CodeBuddyIDE/1.106.1",
    }


async def _claim_codebuddy(token: str, provider_code: str) -> ClaimOutcome:
    """CodeBuddy 两步签到：状态预检 → 领取。

    ⚠️ 必须用 `checkin-activity-status`，不能用 `checkin-status` —— 后者返回
    占位数据（active:false、checkin_dates:null），会误判成「活动未开启」
    （Jet-Hub 记录的踩坑）。
    """
    product = _CODEBUDDY_PRODUCTS.get(provider_code)
    if not product:
        return ClaimOutcome(kind="unsupported", message=f"未知的 CodeBuddy 变体：{provider_code}")
    headers = _codebuddy_headers(token, product)
    base = product["base"]

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # ── 步骤 1：状态预检 ──
        status_url = f"{base}/v2/billing/meter/checkin-activity-status"
        try:
            code, body, text = await _request(client, "POST", status_url, headers, b"{}")
        except Exception as e:
            return ClaimOutcome(kind="failed", message="状态查询失败",
                                error=f"{type(e).__name__}: {str(e)[:200]}")
        if code in (401, 403):
            return ClaimOutcome(kind="failed", message="凭据已失效，请重新登录该账号",
                                error=f"HTTP {code}: {text[:200]}")
        if body is None:
            return ClaimOutcome(kind="failed", message="状态响应不是 JSON",
                                error=text[:300])
        if _num(body.get("code"), -1) != 0:
            return ClaimOutcome(kind="failed",
                                message=f"状态查询错误：{body.get('msg') or 'unknown'}",
                                upstream_code=int(_num(body.get("code"), -1)),
                                error=str(body)[:300])
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        active = _read_bool(data, "active")
        today_checked = _read_bool(data, "today_checked_in")
        streak = _read_int(data, "streak_days")
        total = _num(data.get("total_credits"))
        activity = str(data.get("activity_name") or "")

        if today_checked:
            # 已领 → 不再发 claim 请求（省一次调用，也避免上游风控）
            return ClaimOutcome(kind="already_claimed", streak_days=streak,
                                total_credits=total, activity_name=activity,
                                message="今天已签到", extra={"checkin_dates": data.get("checkin_dates")})
        if not active:
            return ClaimOutcome(kind="inactive", streak_days=streak, total_credits=total,
                                activity_name=activity,
                                message="签到活动未开启（上游 active=false）")

        # ── 步骤 2：领取 ──
        claim_url = f"{base}/v2/billing/meter/daily-checkin"
        try:
            code2, body2, text2 = await _request(client, "POST", claim_url, headers, b"{}")
        except Exception as e:
            return ClaimOutcome(kind="failed", message="领取请求失败",
                                error=f"{type(e).__name__}: {str(e)[:200]}")

    if code2 in (401, 403):
        return ClaimOutcome(kind="failed", message="凭据已失效，请重新登录该账号",
                            error=f"HTTP {code2}: {text2[:200]}")
    if body2 is None:
        return ClaimOutcome(kind="failed", message="领取响应不是 JSON", error=text2[:300])

    bcode = int(_num(body2.get("code"), -1))
    msg = str(body2.get("msg") or "")
    # ⚠️ 判据只看 body code，不看 HTTP 状态（重复领取返回 HTTP 400 + code 10001）
    if bcode in CB_ALREADY:
        return ClaimOutcome(kind="already_claimed", upstream_code=bcode,
                            message=msg or "今天已签到")
    if bcode in (CB_NO_QUALIFICATION, CB_ACTIVITY_ENDED):
        return ClaimOutcome(kind="inactive", upstream_code=bcode,
                            message=msg or "当前无领取资格")
    if bcode != 0:
        return ClaimOutcome(kind="failed", upstream_code=bcode,
                            message=msg or "领取失败", error=str(body2)[:300])

    cdata = body2.get("data") if isinstance(body2.get("data"), dict) else None
    if cdata is None:
        return ClaimOutcome(kind="failed", upstream_code=bcode,
                            message="领取响应缺少 data 字段", error=str(body2)[:300])
    return ClaimOutcome(kind="claimed", credit=_num(cdata.get("credit")),
                        streak_days=_read_int(cdata, "streak_days"),
                        total_credits=total, activity_name=activity,
                        message=msg or "领取成功")


# ── Qoder ────────────────────────────────────────────────────

def _qoder_headers(token: str, with_content_type: bool = False) -> dict:
    """Qoder 积分端点请求头。

    实测（抓包）这 4 个头就是全部所需 —— **不需要 WASM/COSY 签名**
    （那只有推理端点和模型列表要），也不需要 cosy-machine* 那组。
    """
    h = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Cosy-ClientType": "5",   # 源码 Bx() 的固定头，服务端据此区分客户端形态
        "User-Agent": "Qoder",
    }
    if with_content_type:
        h["Content-Type"] = "application/json"
    return h


async def _claim_qoder(token: str, base: str = "https://openapi.qoder.sh") -> ClaimOutcome:
    """Qoder 签到：活动列表 → 逐个领取可领活动。

    ⚠️ 服务端在「今天已领」时返回 {showCampaign:false, campaigns:[]} ——
    「已领」与「本来就没活动」在响应上无法区分，故列表为空时保守判 already_claimed
    （Jet-Hub 记录的踩坑：曾据此误判「Qoder 无签到端点」）。
    """
    headers = _qoder_headers(token)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # ── 步骤 1：活动列表（状态查询，不带 Content-Type）──
        try:
            code, body, text = await _request(client, "GET", f"{base}/sash/api/v1/me/campaigns", headers)
        except Exception as e:
            return ClaimOutcome(kind="failed", message="活动列表查询失败",
                                error=f"{type(e).__name__}: {str(e)[:200]}")
        if code in (401, 403):
            return ClaimOutcome(kind="failed", message="凭据已失效，请重新登录该账号",
                                error=f"HTTP {code}: {text[:200]}")
        if body is None:
            return ClaimOutcome(kind="failed", message="活动列表响应不是 JSON", error=text[:300])

        campaigns = body.get("campaigns") if isinstance(body.get("campaigns"), list) else []
        # 只领 CLAIM_BENEFIT + CLAIMABLE —— 实测还有 VIEW_DETAILS 型（如「Pro 首月翻倍」），
        # 对它发 claim 是错的
        claimable = [c for c in campaigns
                     if isinstance(c, dict)
                     and c.get("actionType") == "CLAIM_BENEFIT"
                     and c.get("claimStatus") == "CLAIMABLE"
                     and c.get("campaignId")]
        if not claimable:
            return ClaimOutcome(kind="already_claimed", message="今天已领取（无可领活动）")

        # ── 步骤 2：逐个领取 ──
        total_credit = 0.0
        claimed_any = False
        already = False
        last_err = ""
        for camp in claimable:
            cid = str(camp.get("campaignId"))
            url = f"{base}/sash/api/v1/me/campaigns/{cid}/claim"
            chdr = _qoder_headers(token, with_content_type=True)
            try:
                # ⚠️ body 必须是空串而非 {}（抓包实测 content-length: 0）
                c2, b2, t2 = await _request(client, "POST", url, chdr, b"")
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:150]}"
                continue
            if c2 in (401, 403):
                return ClaimOutcome(kind="failed", message="凭据已失效，请重新登录该账号",
                                    error=f"HTTP {c2}: {t2[:200]}")
            if b2 is None:
                last_err = f"领取响应不是 JSON: {t2[:150]}"
                continue
            # ⚠️ 幂等判据是 body.replayed，不是 HTTP 状态：重复领取同样返回 200，
            # 但 replayed=true、不含 benefit、claimedAt 是旧时间。只看状态码会把
            # 「今天已领」误报成「领取成功 +100」。
            if b2.get("replayed") is True:
                already = True
                continue
            st = b2.get("status")
            if st is not None and st != "CLAIMED":
                last_err = f"领取未成功（status={st}）"
                continue
            benefit = b2.get("benefit") if isinstance(b2.get("benefit"), dict) else {}
            total_credit += _num(benefit.get("amount"))
            claimed_any = True

    if claimed_any:
        return ClaimOutcome(kind="claimed", credit=total_credit, message="领取成功")
    if already:
        return ClaimOutcome(kind="already_claimed", message="今天已领取")
    return ClaimOutcome(kind="failed", message="领取失败", error=last_err or "上游未返回可识别结果")


def _lobsterai_headers(token: str, client_version: str = "") -> dict:
    """LobsterAI 积分端点请求头。

    只设四个基础头 + 两个客户端头：LobsterAI **不认** CodeBuddy 那套
    X-Domain / X-Product / X-IDE-* 归属头（带上不仅无用，还可能让服务端
    按错误的客户端形态归因 —— Jet-Hub 源码明确记录）。
    """
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "LobsterAI/0.1.0",
        "X-LobsterAI-Client-Capabilities": "kimi-k3-agentic-v1,thinking-level-control-v1",
    }
    if client_version:
        h["X-LobsterAI-Client-Version"] = client_version
    return h


async def _claim_lobsterai(token: str,
                           base: str = "https://lobsterai-server.youdao.com") -> ClaimOutcome:
    """LobsterAI 三步签到：活动槽位 → 活动上下文 → check_in。

    判定顺序（Jet-Hub 源码的「业务正常状态与真失败严格分开」原则）：
    1. 槽位查询失败 → failed
    2. slotState != available 或无 activityCode → inactive
    3. 上下文查询失败 → failed
    4. claimedToday → already_claimed（**不发 check_in 请求**）
    5. actions 不含 check_in → inactive
    6. 领取失败 → failed；成功 → claimed（积分三级回退）

    槽位的三个固定参数是**伪装桌面客户端形态**（照抄 sigin.py:52-53）：
    placement=desktop_sidebar / containerApiVersion=2 / platform=win32 ——
    即使跑在 Linux 上也照发 win32，改了可能拿不到活动。
    """
    import uuid as _uuid

    from server.core import lobsterai as lb   # 复用版本号解析（12h 缓存）
    version = await lb.resolve_client_version()
    headers = _lobsterai_headers(token, version)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # ── 步骤 1：活动槽位 ──
        slot_url = (f"{base}/api/client-activities/slot"
                    f"?placement=desktop_sidebar&clientVersion={version}"
                    f"&containerApiVersion=2&platform=win32")
        try:
            code, body, text = await _request(client, "GET", slot_url, headers)
        except Exception as e:
            return ClaimOutcome(kind="failed", message="活动槽位查询失败",
                                error=f"{type(e).__name__}: {str(e)[:200]}")
        if code in (401, 403):
            return ClaimOutcome(kind="failed", message="凭据已失效，请重新登录该账号",
                                error=f"HTTP {code}: {text[:200]}")
        if body is None:
            return ClaimOutcome(kind="failed", message="活动槽位响应不是 JSON", error=text[:200])
        if _num(body.get("code"), -1) != 0:
            msg = str(body.get("message") or body.get("msg") or "")
            return ClaimOutcome(kind="failed", message=f"活动槽位查询失败：{msg}",
                                upstream_code=int(_num(body.get("code"), -1)),
                                error=str(body)[:200])
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        slot_state = str(data.get("slotState") or "")
        activity = data.get("activity") if isinstance(data.get("activity"), dict) else {}
        activity_code = str(activity.get("activityCode") or "")
        config_rev = _read_int(activity, "configRevision")
        if slot_state != "available" or not activity_code:
            return ClaimOutcome(kind="inactive",
                                message=f"无可用活动（slotState={slot_state or '未知'}）")

        # ── 步骤 2：活动上下文（今天领了没 / 有哪些动作）──
        ctx_url = (f"{base}/api/client-activities/{activity_code}/context"
                   f"?configRevision={config_rev}")
        try:
            code, body, text = await _request(client, "GET", ctx_url, headers)
        except Exception as e:
            return ClaimOutcome(kind="failed", message="活动上下文查询失败",
                                error=f"{type(e).__name__}: {str(e)[:200]}")
        if body is None:
            return ClaimOutcome(kind="failed", message="活动上下文响应不是 JSON", error=text[:200])
        if _num(body.get("code"), -1) != 0:
            msg = str(body.get("message") or body.get("msg") or "")
            return ClaimOutcome(kind="failed", message=f"活动上下文查询失败：{msg}",
                                error=str(body)[:200])
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        if _read_bool(state, "claimedToday"):
            return ClaimOutcome(kind="already_claimed", message="今天已签到")
        actions = data.get("actions") if isinstance(data.get("actions"), list) else []
        if "check_in" not in actions:
            # 活动存在但当前不可签（未开始/已结束/无资格）——与「今天已领」区分
            return ClaimOutcome(kind="inactive", message="当前不可签到")

        # ── 步骤 3：签到（客户端幂等键，对齐 sigin.py:63 的 uuid4）──
        claim_url = f"{base}/api/client-activities/{activity_code}/actions/check_in"
        payload = json.dumps({
            "configRevision": config_rev,
            "idempotencyKey": str(_uuid.uuid4()),
            "payload": {},
        }).encode()
        try:
            code, body, text = await _request(client, "POST", claim_url, headers, payload)
        except Exception as e:
            return ClaimOutcome(kind="failed", message="签到请求失败",
                                error=f"{type(e).__name__}: {str(e)[:200]}")
        if code in (401, 403):
            return ClaimOutcome(kind="failed", message="凭据已失效，请重新登录该账号",
                                error=f"HTTP {code}: {text[:200]}")
        if body is None:
            return ClaimOutcome(kind="failed", message="签到响应不是 JSON", error=text[:200])
        if _num(body.get("code"), -1) != 0:
            msg = str(body.get("message") or body.get("msg") or "")
            return ClaimOutcome(kind="failed", message=f"签到失败：{msg}",
                                upstream_code=int(_num(body.get("code"), -1)),
                                error=str(body)[:200])
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        # 积分字段三级回退（对齐 sigin.py:65-66）：不同活动/版本用不同字段名
        credit = 0.0
        for key in ("creditsGranted", "rewardCredits", "credits"):
            v = result.get(key)
            if isinstance(v, (int, float)) and v == v:
                credit = float(v)
                break
        return ClaimOutcome(kind="claimed", credit=credit,
                            activity_name=activity_code, message="签到成功")


# ── CodeArts（华为）四步签到 ─────────────────────────────────

async def _claim_codearts(token: str) -> ClaimOutcome:
    """CodeArts 每日签到（四步）。token 是**凭据 JSON**（AK/SK/SecurityToken）。

    ⚠️ 与其它 provider 的 token 语义不同：这里不是 Bearer，是签名材料。
    `collect_targets` 从 access_token 列解出的正是这段 JSON，直接透传即可。
    """
    import json as _json
    from server.core import codearts as ca
    try:
        cred = _json.loads(token)
    except (ValueError, TypeError):
        return ClaimOutcome(kind="failed", message="凭据格式不对（应为 JSON 凭据）")
    if not isinstance(cred, dict) or not cred.get("access_key_id"):
        return ClaimOutcome(kind="failed", message="凭据缺少 AK（请重新连接该账号）")
    out = await ca.claim_daily_checkin(cred)
    kind = str(out.get("kind") or "failed")
    return ClaimOutcome(
        kind=kind if kind in ("claimed", "already_claimed", "inactive", "failed") else "failed",
        credit=float(out.get("credit") or 0.0),
        activity_name=str(out.get("activity_name") or ""),
        message=str(out.get("message") or ""),
        error=str(out.get("error") or ""),
    )


# ── TRAE（字节）两步签到 ─────────────────────────────────────

async def _trae_meta() -> dict:
    """读 trae 连接 scope 列里的身份字段（uid/domain）。"""
    try:
        from server.db import AsyncSessionLocal
        from server.core.oauth_client import get_oauth_client
        async with AsyncSessionLocal() as db:
            return await get_oauth_client().get_token_meta("trae", db)
    except Exception:
        return {}


async def _claim_trae(token: str) -> ClaimOutcome:
    """Trae 每日签到（两步协议）。

    ⚠️ 三个不可省的点（Jet-Hub 实测，见 server/core/trae.py 的 claim_checkin）：
    1. **必须先查 status** —— claim 对「今天已签到」幂等返回 code:0，无法区分，
       不预检会把已签到的账号报成「领取成功」；
    2. claim 响应**不含积分数**，领取后必须补查 status 取真实所得，
       否则恒定显示「+0 积分」；
    3. 9074 归为业务错误（300s 冷却），**不换设备号重试**。
    """
    from server.core import trae as tr
    meta = await _trae_meta()
    uid = str(meta.get("uid") or "")
    if not uid:
        return ClaimOutcome(kind="failed", message="缺少 uid，无法构造签到设备身份")
    out = await tr.claim_checkin({"access_token": token, "uid": uid},
                                 str(meta.get("domain") or ""))
    kind = str(out.get("kind") or "failed")
    return ClaimOutcome(
        kind=kind if kind in ("claimed", "already_claimed", "inactive", "failed") else "failed",
        credit=float(out.get("credit") or 0.0),
        streak_days=int(out.get("streak_days") or 0),
        activity_name="每日签到",
        message=str(out.get("message") or ""),
        error=str(out.get("error") or ""),
        upstream_code=out.get("upstream_code"),
    )


# ── 分发 ─────────────────────────────────────────────────────
async def claim_for_provider(provider_code: str, token: str) -> ClaimOutcome:
    """按 provider 分发签到。永不抛异常，失败以 kind=failed 表达。"""
    cap = CHECKIN_CAPABILITIES.get(provider_code)
    if cap is False:
        return ClaimOutcome(kind="unsupported", message="该平台上游未提供签到接口")
    try:
        if provider_code in _CODEBUDDY_PRODUCTS:
            return await _claim_codebuddy(token, provider_code)
        if provider_code == "qoder":
            return await _claim_qoder(token)
        if provider_code == "lobsterai":
            return await _claim_lobsterai(token)
        if provider_code == "codearts":
            return await _claim_codearts(token)
        if provider_code == "trae":
            return await _claim_trae(token)
        return ClaimOutcome(kind="unsupported", message=f"未实现签到适配器：{provider_code}")
    except Exception as e:   # 防御：任何解析异常都不冒穿（照 oauth_usage 的做法）
        logger.warning("checkin error for %s: %s", provider_code, e)
        return ClaimOutcome(kind="failed", message="签到异常",
                            error=f"{type(e).__name__}: {str(e)[:200]}")


# ── 批量执行（严格串行）───────────────────────────────────────

@dataclass
class CheckinTarget:
    """一个待签到的账号。"""
    provider_code: str
    owner: str
    token: str


async def run_checkin_batch(targets: List[CheckinTarget], *, trigger: str = "manual",
                            db=None, notify: bool = True) -> List[dict]:
    """顺序执行一批签到，逐个落 checkin_logs。

    **严格串行**：并发易触发上游风控（Jet-Hub 三处注释 + 单测锁死）。
    单账号失败不中断整批。
    """
    results: List[dict] = []
    failures: List[str] = []
    for t in targets:
        t0 = time.monotonic()
        outcome = await claim_for_provider(t.provider_code, t.token)
        duration_ms = int((time.monotonic() - t0) * 1000)
        rec = {
            "provider_code": t.provider_code, "owner": t.owner, "kind": outcome.kind,
            "credit": outcome.credit if outcome.kind == "claimed" else None,
            "streak_days": outcome.streak_days or None,
            "total_credits": outcome.total_credits or None,
            "activity_name": outcome.activity_name or "",
            "message": outcome.message, "error": outcome.error or None,
            "upstream_code": outcome.upstream_code, "duration_ms": duration_ms,
        }
        results.append(rec)
        if outcome.kind == "failed":
            failures.append(f"{t.provider_code}/{t.owner}: {outcome.message}"
                            + (f"（{outcome.error}）" if outcome.error else ""))
        logger.info("checkin %s/%s -> %s credit=%s (%dms)",
                    t.provider_code, t.owner, outcome.kind, outcome.credit, duration_ms)
        # 落库（独立 session，避免与调用方事务纠缠）
        if db is not None:
            try:
                from server.models.checkin_log import CheckinLog
                row = CheckinLog(trigger=trigger, **rec)
                db.add(row)
                await db.commit()
            except Exception as e:
                logger.warning("checkin log write failed: %s", e)
                try:
                    await db.rollback()
                except Exception:
                    pass

    if notify and failures:
        try:
            from server.core.notifier import notify_event
            head = f"签到失败 {len(failures)}/{len(targets)} 个账号："
            notify_event("checkin", head + "；".join(failures[:5]))
        except Exception:
            pass
    return results


# ── 幂等辅助：今日是否已完成 ─────────────────────────────────

async def already_done_today(db, provider_code: str, owner: str) -> bool:
    """该账号今天是否已尝试过签到（claimed/already_claimed/inactive）。

    定时任务与启动补签据此幂等跳过：已有记录就不再发上游请求（上游自身幂等是
    第二道防线）。failed **不算**——失败必须允许下次重试。
    按**北京时间**划分「今天」——上游活动按 UTC+8 刷新（Qoder 每日 10:00）。
    """
    from sqlalchemy import select, and_
    from server.models.checkin_log import CheckinLog
    now_cst = datetime.now(CST)
    day_start_cst = now_cst.replace(hour=0, minute=0, second=0, microsecond=0)
    # 库内 created_at 是 naive UTC → 换算窗口
    day_start_utc = day_start_cst.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        row = (await db.execute(
            select(CheckinLog.id).where(and_(
                CheckinLog.provider_code == provider_code,
                CheckinLog.owner == owner,
                CheckinLog.kind.in_(ATTEMPTED_KINDS),
                CheckinLog.created_at >= day_start_utc,
            )).limit(1)
        )).first()
        return row is not None
    except Exception:
        return False


async def collect_targets(db) -> Tuple[List[CheckinTarget], List[dict]]:
    """收集所有可签到的 (provider, owner) 及其 token。

    返回 (targets, skipped)：skipped 记录因能力不支持/无凭据/今日已完成而跳过的项，
    供 UI 展示完整账号列表（签到页要显示所有账号，包括跳过原因）。
    """
    from sqlalchemy import select
    from server.models.oauth_token import OAuthToken
    from server.core.oauth_client import get_oauth_client

    client = get_oauth_client()
    targets: List[CheckinTarget] = []
    skipped: List[dict] = []
    codes = [c for c, v in CHECKIN_CAPABILITIES.items()]
    rows = (await db.execute(
        select(OAuthToken).where(
            OAuthToken.is_active.is_(True),
            OAuthToken.provider_code.in_(codes),
        ).order_by(OAuthToken.provider_code, OAuthToken.id)
    )).scalars().all()

    for r in rows:
        cap = CHECKIN_CAPABILITIES.get(r.provider_code)
        if cap is not True:
            skipped.append({"provider_code": r.provider_code, "owner": r.owner,
                            "reason": "unsupported" if cap is False else "pending_probe"})
            continue
        if await already_done_today(db, r.provider_code, r.owner):
            skipped.append({"provider_code": r.provider_code, "owner": r.owner,
                            "reason": "already_done_today"})
            continue
        try:
            token = client._crypto.decrypt(r.access_token_enc)
        except Exception:
            skipped.append({"provider_code": r.provider_code, "owner": r.owner,
                            "reason": "decrypt_failed"})
            continue
        if not token:
            skipped.append({"provider_code": r.provider_code, "owner": r.owner,
                            "reason": "empty_token"})
            continue
        targets.append(CheckinTarget(provider_code=r.provider_code, owner=r.owner, token=token))
    return targets, skipped
