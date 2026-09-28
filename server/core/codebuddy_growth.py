"""CodeBuddy 成长中心（Buddy 旅行 / 成长任务 / 连续活跃）—— 纯 HTTP，无桌面依赖。

来源与依据（2026-09-28 调研，见 `docs/findings-workdaddy-features.md`）：
功能形态来自 WorkDaddy（WorkBuddy 桌面增强工具，CDP 注入），但**这些端点本身
是纯 HTTP API**，与桌面端无关 —— 生产实测（CodeBuddy 账号 + 标准头）全部 200，
且 workbuddy.cn ≡ codebuddy.cn 是同一后端（见 findings §F24）。

## 能力清单（实测）

    GET  /activity/growth/tasks              成长任务列表（含 reward/progress）
    POST /activity/growth/tasks/accept       批量接取任务（body: {task_codes:[...]})
    GET  /activity/growth/streak             连续活跃 + 7/14/28 天阶梯 + 补签卡
    GET  /activity/growth/buddy/info         当前出战 Buddy
    GET  /activity/growth/buddy/list         已拥有 Buddy 列表
    POST /activity/growth/buddy/switch       切换出战 Buddy（body: {instance_id})
    GET  /activity/growth/buddy/quota        盲盒额度（affordable/balance/cost_per_open）
    GET  /activity/growth/lottery/chances    抽奖次数
    GET  /v2/activity/growth/buddy/travel/config    旅行地点列表
    GET  /activity/growth/buddy/travel/status       旅行状态（idle/traveling/arrived）
    POST /v2/activity/growth/buddy/travel/depart    派出（body: {location_id})
    POST /activity/growth/buddy/travel/claim        到达后领奖

## 三条设计约束（照抄上游做法，不发明）

1. **只自动做「客户端外能完成」的事**：WorkDaddy 源码有 `AUTOMATABLE_TASK_CODES`
   白名单 —— 其余任务（关注公众号、体验公益专家等）需要真实点击，自动化必失败。
   所以本模块**只接取白名单内的任务**，其余如实列给用户手动做。
2. **严格串行 + 按天幂等**：上游对单账号高频请求敏感（同 checkin 的克制做法）；
   旅行按北京时间当天去重，避免重复派发。
3. **只读优先、失败不编造**：拿不到就如实显示「不可用」，绝不假设状态。

## 实测注意

- `instance_id` 是数字，上游要求纯数字串（WorkDaddy 强制 `Number()` 且校验
  `String(x) === String(id)`）—— 照抄该校验，防字符串/数字混用。
- 国际版**本版无此活动**：端点全部 200 但数据为空（`buddy:null`、`buddies:[]`、
  `travel.config:{}`）→ 如实显示，不报错。
- `/profile/plans-usage` 是 HTML 页面（WorkDaddy 靠 CDP 读 DOM）→ **不迁移**，
  额度仍走 AIGate 既有的 billing API。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 20.0
CST = timezone(timedelta(hours=8))

# ── 产品配置（与 checkin.py 同口径：X-Domain 必须跟请求的 base 走）──────
GROWTH_PRODUCTS: Dict[str, dict] = {
    "codebuddy_cn": {"host": "copilot.tencent.com", "base": "https://copilot.tencent.com"},
    "codebuddy_intl": {"host": "www.codebuddy.ai", "base": "https://www.codebuddy.ai"},
}

# ── 可自动化任务白名单（照抄 WorkDaddy 的 AUTOMATABLE_TASK_CODES）──────
# 只有这些任务码**能在客户端外完成**；其余需要真实点击/关注，自动化必失败。
# 迁移原则：只接白名单内的，其余如实列给用户手动做（对齐「如实标注不过滤」）。
AUTOMATABLE_TASK_CODES = frozenset({
    "create_canvas", "template_5", "expert_5", "Expert_team_use_3", "automation_1",
    "playbook_prompt", "Expert_lighthouse", "Buddy_App", "Buddy_App_QQ",
    "Hp_Appearance", "chat_5", "Model_chat_GLM5.2", "black_cat", "Library_read",
})
# 已领/已完成态（不重复接取）
_TASK_DONE_STATES = frozenset({"claimed", "completed"})
# 上游要求 task_code 形态（防注入；照抄 WorkDaddy 的正则）
_TASK_CODE_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


def _task_code_ok(code: str) -> bool:
    """任务码白名单校验（形态 + 长度，防注入）。非字符串一律拒绝。"""
    if not isinstance(code, str) or not code or len(code) > 96:
        return False
    return all(c in _TASK_CODE_SAFE for c in code)


def _num(v, fallback: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else fallback
    except (TypeError, ValueError):
        return fallback


def _int(v, fallback: int = 0) -> int:
    return int(_num(v, fallback))


def _safe_int_id(v) -> Optional[int]:
    """上游 id 校验（照抄 WorkDaddy）：必须纯数字串，防字符串/数字混用。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, str) and v.strip() and v.strip().isdigit():
        n = int(v.strip())
        return n if n > 0 else None
    return None


# ── 结果类型 ─────────────────────────────────────────────────

@dataclass
class GrowthSnapshot:
    """一个账号的成长中心只读快照（旅行 + 任务 + 活跃 + 盲盒/抽奖）。"""
    ok: bool = False
    message: str = ""                       # 不可用时的原因（如实说明）
    # 旅行
    travel_state: str = "unknown"           # idle | traveling | arrived | locked | unknown
    travel_location: str = ""               # 地点名（如「咖啡馆」）
    travel_arrive_at: Optional[int] = None  # 到达时间戳（秒）
    travel_daily_limit: bool = False
    travel_reward_credit: float = 0.0
    # Buddy
    buddy_name: str = ""
    buddy_rarity: str = ""
    buddy_instance_id: Optional[int] = None
    buddy_count: int = 0
    # 任务
    tasks: List[dict] = field(default_factory=list)   # 见 _task_view
    task_completed: int = 0
    task_total: int = 0
    task_claimable: float = 0.0             # 未领奖励合计（credits）
    manual_tasks: List[str] = field(default_factory=list)  # 需用户手动做的
    # 活跃
    streak_days: int = 0
    streak_next_tier: str = ""
    streak_next_remaining: int = 0
    makeup_cards: int = 0
    tiers: List[dict] = field(default_factory=list)
    # 盲盒/抽奖
    gacha_affordable: int = 0
    gacha_balance: int = 0
    gacha_cost: int = 0
    lottery_chances: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "message": self.message,
            "travel": {
                "state": self.travel_state, "location": self.travel_location,
                "arrive_at": self.travel_arrive_at,
                "daily_limit_reached": self.travel_daily_limit,
                "reward_credit": self.travel_reward_credit,
            },
            "buddy": {
                "name": self.buddy_name, "rarity": self.buddy_rarity,
                "instance_id": self.buddy_instance_id, "count": self.buddy_count,
            },
            "tasks": self.tasks,
            "task_summary": {
                "completed": self.task_completed, "total": self.task_total,
                "claimable_credits": self.task_claimable,
            },
            "manual_tasks": self.manual_tasks,
            "streak": {
                "days": self.streak_days, "next_tier": self.streak_next_tier,
                "next_tier_remaining": self.streak_next_remaining,
                "makeup_cards": self.makeup_cards, "tiers": self.tiers,
            },
            "gacha": {"affordable": self.gacha_affordable, "balance": self.gacha_balance,
                      "cost_per_open": self.gacha_cost},
            "lottery": {"chances": self.lottery_chances},
        }


@dataclass
class GrowthAction:
    """一次写操作的结果（派发旅行 / 领奖 / 接任务）。"""
    kind: str                 # traveled | claimed | accepted | already | inactive | unsupported | failed
    credit: float = 0.0
    energy: float = 0.0
    message: str = ""
    error: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind in ("traveled", "claimed", "accepted", "already")


# ── 请求头（照抄 WorkDaddy 的 web 形态）──────────────────────

def growth_headers(token: str, product: dict) -> dict:
    """成长中心请求头。

    WorkDaddy 实测用 **web 形态**（`origin`/`referer: /profile/growth-center` +
    `x-client-platform: web`）访问这些端点；CodeBuddy 标准头（X-Domain 等）也带上，
    两者兼容（生产实测全部 200）。
    """
    host = product["host"]
    origin = f"https://www.workbuddy.cn" if "tencent" in host else f"https://{host}"
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Domain": host,
        "X-Product": "SaaS",
        "X-Product-Code": "codebuddy",
        "User-Agent": "CLI/2.108.1 CodeBuddy/2.108.1",
        "x-codebuddy-request": "1",
        "x-client-platform": "web",
        "origin": origin,
        "referer": f"{origin}/profile/growth-center",
    }


async def _get(client: httpx.AsyncClient, url: str, headers: dict):
    """GET 并返回 (status, data_or_None, raw)。

    统一先取 text 再 parse —— 401/403 可能返回 HTML（CodeBuddy 实测）。
    """
    r = await client.get(url, headers=headers)
    try:
        parsed = r.json()
    except Exception:
        parsed = None
    return r.status_code, parsed, r.text


async def _post(client: httpx.AsyncClient, url: str, headers: dict, body: dict):
    import json as _json
    r = await client.post(url, headers=headers,
                          content=_json.dumps(body, ensure_ascii=False).encode())
    try:
        parsed = r.json()
    except Exception:
        parsed = None
    return r.status_code, parsed, r.text


def _payload_ok(status: int, body) -> Tuple[bool, str, dict]:
    """统一判据：HTTP 2xx 且 body.code == 0 → (True, '', data)。否则 (False, 原因, {})。"""
    if status in (401, 403):
        return False, "凭据已失效，请重新登录该账号", {}
    if not isinstance(body, dict):
        return False, f"响应不是 JSON（HTTP {status}）", {}
    if _int(body.get("code"), -1) != 0:
        return False, str(body.get("msg") or f"上游 code={body.get('code')}")[:200], {}
    data = body.get("data")
    return True, "", data if isinstance(data, dict) else {}


# ── 只读快照 ─────────────────────────────────────────────────

def _task_view(t: dict) -> dict:
    """单个任务的展示视图（如实映射上游三态，不合并）。"""
    status = str(t.get("accept_status") or "not_accepted")
    prog = t.get("progress") if isinstance(t.get("progress"), dict) else {}
    cur = max(0, _int(prog.get("current")))
    tgt = max(1, _int(prog.get("target"), 1))
    code = str(t.get("task_code") or "")
    complete = status in _TASK_DONE_STATES or cur >= tgt
    state = ("claimed" if status == "claimed"
             else "completed" if complete
             else "not_accepted" if status == "not_accepted"
             else "in_progress")
    return {
        "code": code,
        "title": str(t.get("title") or t.get("task_desc") or code or "未识别任务")[:80],
        "guide": str(t.get("description") or t.get("task_desc") or "")[:360],
        "tag": str(t.get("tag") or "")[:24],
        "deadline": str(t.get("valid_end") or "") or None,
        "credit": _num(t.get("reward_credit")),
        "energy": _num(t.get("reward_energy")),
        "current": cur, "target": tgt, "state": state,
        # 能否由本模块自动完成（白名单 + 未完成）
        "automatable": code in AUTOMATABLE_TASK_CODES and not complete,
    }


async def fetch_snapshot(token: str, provider_code: str) -> GrowthSnapshot:
    """拉一个账号的成长中心只读快照（多次 GET，全部只读、0 消耗）。

    任一子请求失败不致命：能拿到多少填多少，`ok` 表示**至少拿到一项**。
    国际版本版无此活动（数据为空）→ ok=True 但各字段为初始值，UI 如实显示。
    """
    product = GROWTH_PRODUCTS.get(provider_code)
    if not product:
        return GrowthSnapshot(ok=False, message=f"未知的 CodeBuddy 变体：{provider_code}")
    base = product["base"]
    headers = growth_headers(token, product)
    snap = GrowthSnapshot()
    got_any = False
    first_err = ""

    try:
        client_cm = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    except Exception as e:  # 客户端构造失败（代理配置等）也要如实报告
        return GrowthSnapshot(ok=False, message=f"HTTP 客户端初始化失败：{type(e).__name__}")

    try:
        async with client_cm as client:
            snap, got_any, first_err = await _collect_snapshot(client, base, headers, snap)
    except Exception as e:
        # 连接层异常（DNS/代理/连接被拒）—— 全部子请求都会失败，统一如实报告
        if not got_any:
            return GrowthSnapshot(ok=False, message=f"{type(e).__name__}: {str(e)[:150]}")

    snap.ok = got_any
    if not got_any:
        snap.message = first_err or "成长中心不可用（上游无响应）"
    return snap


async def _collect_snapshot(client: httpx.AsyncClient, base: str, headers: dict,
                        snap: GrowthSnapshot) -> Tuple[GrowthSnapshot, bool, str]:
    """在给定 client 上收集快照（拆出便于统一包住连接层异常）。"""
    got_any = False
    first_err = ""
    # 1) 旅行状态
    try:
        st, body, _ = await _get(client, f"{base}/activity/growth/buddy/travel/status", headers)
        ok, err, d = _payload_ok(st, body)
        if ok:
            got_any = True
            snap.travel_state = str(d.get("state") or "unknown")
            loc = d.get("location") if isinstance(d.get("location"), dict) else {}
            snap.travel_location = str(loc.get("name") or "")
            arrive = _num(d.get("arrive_at"))
            snap.travel_arrive_at = int(arrive) if arrive > 0 else None
            snap.travel_daily_limit = d.get("daily_limit_reached") is True
            snap.travel_reward_credit = _num(d.get("reward_credit"))
        elif not first_err:
            first_err = err
    except Exception as e:
        if not first_err:
            first_err = f"{type(e).__name__}: {str(e)[:120]}"

    # 2) 当前 Buddy + 拥有列表
    try:
        st, body, _ = await _get(client, f"{base}/activity/growth/buddy/info", headers)
        ok, err, d = _payload_ok(st, body)
        if ok:
            got_any = True
            b = d.get("buddy") if isinstance(d.get("buddy"), dict) else {}
            if b:
                snap.buddy_name = str(b.get("name") or "")
                snap.buddy_rarity = str(b.get("rarity") or "")
                snap.buddy_instance_id = _safe_int_id(b.get("instance_id"))
        elif not first_err:
            first_err = err
        st, body, _ = await _get(client, f"{base}/activity/growth/buddy/list", headers)
        ok, err, d = _payload_ok(st, body)
        if ok:
            got_any = True
            buddies = d.get("buddies") if isinstance(d.get("buddies"), list) else []
            snap.buddy_count = len(buddies)
            if not snap.buddy_instance_id and buddies:
                snap.buddy_instance_id = _safe_int_id(buddies[0].get("instance_id"))
    except Exception as e:
        if not first_err:
            first_err = f"{type(e).__name__}: {str(e)[:120]}"

    # 3) 成长任务（v2 有完整 reward/progress）
    try:
        st, body, _ = await _get(client, f"{base}/v2/activity/growth/tasks", headers)
        ok, err, d = _payload_ok(st, body)
        if not ok:
            st, body, _ = await _get(client, f"{base}/activity/growth/tasks", headers)
            ok, err, d = _payload_ok(st, body)
        if ok:
            got_any = True
            raw = d.get("tasks") if isinstance(d.get("tasks"), list) else []
            views = [_task_view(t) for t in raw if isinstance(t, dict)]
            snap.tasks = views
            snap.task_total = len(views)
            snap.task_completed = sum(1 for v in views if v["state"] in _TASK_DONE_STATES)
            snap.task_claimable = sum(
                v["credit"] for v in views
                if v["state"] not in _TASK_DONE_STATES and v["credit"] > 0)
            snap.manual_tasks = [
                v["title"] for v in views
                if v["state"] not in _TASK_DONE_STATES and not v["automatable"]][:8]
        elif not first_err:
            first_err = err
    except Exception as e:
        if not first_err:
            first_err = f"{type(e).__name__}: {str(e)[:120]}"

    # 4) 连续活跃 + 阶梯
    try:
        st, body, _ = await _get(client, f"{base}/activity/growth/streak", headers)
        ok, err, d = _payload_ok(st, body)
        if ok:
            got_any = True
            sk = d.get("streak") if isinstance(d.get("streak"), dict) else {}
            snap.streak_days = _int(sk.get("days"))
            snap.streak_next_tier = str(sk.get("next_tier") or "")
            snap.streak_next_remaining = _int(sk.get("next_tier_remaining"))
            cards = d.get("makeup_cards") if isinstance(d.get("makeup_cards"), dict) else {}
            snap.makeup_cards = _int(cards.get("balance"))
            rs = d.get("redemption_status") if isinstance(d.get("redemption_status"), dict) else {}
            tiers = rs.get("tiers") if isinstance(rs.get("tiers"), list) else []
            snap.tiers = [{
                "tier": str(t.get("tier") or ""), "days": _int(t.get("days")),
                "credit": _num(t.get("credit")), "energy": _num(t.get("energy")),
                "cards": _int(t.get("cards")), "chances": _int(t.get("chances")),
            } for t in tiers if isinstance(t, dict)]
        elif not first_err:
            first_err = err
    except Exception as e:
        if not first_err:
            first_err = f"{type(e).__name__}: {str(e)[:120]}"

    # 5) 盲盒额度 + 抽奖次数（只读）
    try:
        st, body, _ = await _get(client, f"{base}/activity/growth/buddy/quota", headers)
        ok, err, d = _payload_ok(st, body)
        if ok:
            got_any = True
            snap.gacha_affordable = _int(d.get("affordable"))
            snap.gacha_balance = _int(d.get("balance"))
            snap.gacha_cost = _int(d.get("cost_per_open"))
        st, body, _ = await _get(client, f"{base}/activity/growth/lottery/chances", headers)
        ok, err, d = _payload_ok(st, body)
        if ok:
            got_any = True
            snap.lottery_chances = _int(d.get("balance"))
    except Exception:
        pass

    return snap, got_any, first_err


# ── 写操作：旅行 ─────────────────────────────────────────────

async def travel_depart(token: str, provider_code: str,
                        location_id: Optional[int] = None) -> GrowthAction:
    """派出 Buddy 旅行。未指定地点时取 config 里的第一个可用地点。

    上游会按天限制次数（status 里的 daily_limit_reached）—— 调用方负责当天去重。
    """
    product = GROWTH_PRODUCTS.get(provider_code)
    if not product:
        return GrowthAction(kind="unsupported", message=f"未知的 CodeBuddy 变体：{provider_code}")
    base = product["base"]
    headers = growth_headers(token, product)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            return await _travel_depart_inner(client, base, headers, location_id)
    except Exception as e:
        # 连接层异常（DNS/代理/连接被拒）统一转 failed，绝不穿透
        return GrowthAction(kind="failed", message="派出请求失败",
                            error=f"{type(e).__name__}: {str(e)[:200]}")


async def _travel_depart_inner(client, base, headers, location_id) -> GrowthAction:
    """travel_depart 主体（在已建立的 client 上执行）。"""
    if location_id is None:
        try:
            st, body, _ = await _get(
                client, f"{base}/v2/activity/growth/buddy/travel/config", headers)
        except Exception as e:
            return GrowthAction(kind="failed", message="旅行地点查询失败",
                                error=f"{type(e).__name__}: {str(e)[:200]}")
        ok, err, d = _payload_ok(st, body)
        if not ok:
            return GrowthAction(kind="failed", message=f"旅行地点查询失败：{err}")
        locs = d.get("locations") if isinstance(d.get("locations"), list) else []
        if not locs:
            return GrowthAction(kind="inactive",
                                message="暂无可用旅行地点（本版无此活动或已全部走完）")
        location_id = _safe_int_id(locs[0].get("id"))
        if location_id is None:
            return GrowthAction(kind="failed", message="旅行地点 id 非法（上游返回非数字）")
    st, body, _ = await _post(
        client, f"{base}/v2/activity/growth/buddy/travel/depart",
        headers, {"location_id": location_id})
    ok, err, d = _payload_ok(st, body)
    if not ok:
        return GrowthAction(kind="failed", message=f"派出失败：{err}", error=err)
    return GrowthAction(kind="traveled", message="已派出旅行",
                        detail={"location_id": location_id, "raw": d})


async def travel_claim(token: str, provider_code: str) -> GrowthAction:
    """领取旅行礼物（仅 state == arrived 时可领）。"""
    product = GROWTH_PRODUCTS.get(provider_code)
    if not product:
        return GrowthAction(kind="unsupported", message=f"未知的 CodeBuddy 变体：{provider_code}")
    base = product["base"]
    headers = growth_headers(token, product)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            st, body, _ = await _post(
                client, f"{base}/activity/growth/buddy/travel/claim", headers, {})
    except Exception as e:
        return GrowthAction(kind="failed", message="领取请求失败",
                            error=f"{type(e).__name__}: {str(e)[:200]}")
    ok, err, d = _payload_ok(st, body)
    if not ok:
        return GrowthAction(kind="failed", message=f"领取失败：{err}", error=err)
    return GrowthAction(
        kind="claimed",
        credit=_num(d.get("reward_credit") or d.get("credit_granted")),
        energy=_num(d.get("reward_energy") or d.get("energy_granted")),
        message="旅行礼物已领取", detail={"raw": d})


async def run_travel(token: str, provider_code: str) -> GrowthAction:
    """旅行一步到位：按状态决定「领奖 / 派发 / 什么都不做」。

    状态机（上游）：
      arrived            → claim（领奖）
      idle 且未达上限     → depart（派出）
      traveling / locked → 不动（等到达 / 本版无活动）
    """
    snap = await fetch_snapshot(token, provider_code)
    if not snap.ok:
        return GrowthAction(kind="failed", message=snap.message or "成长中心不可用")
    if snap.travel_state == "arrived":
        return await travel_claim(token, provider_code)
    if snap.travel_state == "idle":
        if snap.travel_daily_limit:
            return GrowthAction(kind="already", message="今日旅行次数已用完")
        if not snap.buddy_instance_id:
            return GrowthAction(kind="inactive",
                                message="尚未领养/选择 Buddy（需先在官网领养），跳过旅行")
        return await travel_depart(token, provider_code)
    if snap.travel_state in ("traveling",):
        return GrowthAction(kind="already", message="旅行进行中（等待到达）")
    if snap.travel_state in ("locked", "unknown"):
        return GrowthAction(kind="inactive",
                            message="本版无旅行活动（上游返回空配置）")
    return GrowthAction(kind="already", message=f"旅行状态 {snap.travel_state}，无需操作")


# ── 写操作：成长任务 ─────────────────────────────────────────

async def accept_tasks(token: str, provider_code: str,
                       codes: List[str]) -> GrowthAction:
    """批量接取成长任务（**只应传白名单内的码**，调用方过滤）。"""
    product = GROWTH_PRODUCTS.get(provider_code)
    if not product:
        return GrowthAction(kind="unsupported", message=f"未知的 CodeBuddy 变体：{provider_code}")
    valid = [c for c in dict.fromkeys(codes or []) if _task_code_ok(c)][:20]
    if not valid:
        return GrowthAction(kind="inactive", message="没有可接取的任务")
    base = product["base"]
    headers = growth_headers(token, product)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            st, body, _ = await _post(client, f"{base}/activity/growth/tasks/accept",
                                      headers, {"task_codes": valid})
    except Exception as e:
        return GrowthAction(kind="failed", message="接取请求失败",
                            error=f"{type(e).__name__}: {str(e)[:200]}")
    ok, err, d = _payload_ok(st, body)
    if not ok:
        return GrowthAction(kind="failed", message=f"接取失败：{err}", error=err)
    return GrowthAction(kind="accepted", message=f"已接取 {len(valid)} 个任务",
                        detail={"accepted": valid, "raw": d})


async def accept_automatable_tasks(token: str, provider_code: str) -> GrowthAction:
    """接取该账号所有「白名单内 + 未完成 + 未接取」的任务。

    这是唯一安全的自动写操作：白名单外的任务自动化必失败（见模块 docstring），
    只做接取（不改任务状态、不伪造完成）。
    """
    snap = await fetch_snapshot(token, provider_code)
    if not snap.ok:
        return GrowthAction(kind="failed", message=snap.message or "成长中心不可用")
    pending = [t["code"] for t in snap.tasks
               if t.get("automatable") and t.get("state") == "not_accepted"]
    if not pending:
        return GrowthAction(kind="already", message="没有待接取的可自动化任务")
    return await accept_tasks(token, provider_code, pending)


# ── 只读辅助：盲盒 / 抽奖（暂不自动执行，仅展示）──────────────

async def fetch_lottery_chances(token: str, provider_code: str) -> int:
    product = GROWTH_PRODUCTS.get(provider_code)
    if not product:
        return 0
    headers = growth_headers(token, product)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            st, body, _ = await _get(
                client, f"{product['base']}/activity/growth/lottery/chances", headers)
        ok, _, d = _payload_ok(st, body)
        return _int(d.get("balance")) if ok else 0
    except Exception:
        return 0


# ── 批量执行（定时 / 手动共用）─────────────────────────────────

@dataclass
class GrowthTarget:
    """一个待处理的成长中心账号。"""
    provider_code: str
    owner: str
    token: str


async def _already_done_today(db, provider_code: str, owner: str, action: str) -> bool:
    """该账号今天是否已处理过该动作（travel / task）。

    与签到同口径：按**北京时间**划分「今天」（上游活动按 UTC+8 刷新）。
    failed **不算** —— 失败必须允许下次重试。
    """
    from sqlalchemy import select, and_
    from server.models.growth_log import GrowthLog
    now_cst = datetime.now(CST)
    day_start_cst = now_cst.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_cst.astimezone(timezone.utc).replace(tzinfo=None)
    done_kinds = ("traveled", "claimed", "accepted", "already", "inactive")
    try:
        row = (await db.execute(
            select(GrowthLog.id).where(and_(
                GrowthLog.provider_code == provider_code,
                GrowthLog.owner == owner,
                GrowthLog.action == action,
                GrowthLog.kind.in_(done_kinds),
                GrowthLog.created_at >= day_start_utc,
            )).limit(1)
        )).first()
        return row is not None
    except Exception:
        return False


async def collect_growth_targets(db) -> Tuple[List[GrowthTarget], List[dict]]:
    """收集可处理的成长中心账号（仅 CodeBuddy 两版）。

    skipped 记录因能力不支持/无凭据/今日已处理而跳过的项，供 UI 展示完整列表。
    """
    from sqlalchemy import select
    from server.models.oauth_token import OAuthToken
    from server.core.oauth_client import get_oauth_client

    client = get_oauth_client()
    targets: List[GrowthTarget] = []
    skipped: List[dict] = []
    rows = (await db.execute(
        select(OAuthToken).where(
            OAuthToken.is_active.is_(True),
            OAuthToken.provider_code.in_(list(GROWTH_PRODUCTS)),
        ).order_by(OAuthToken.provider_code, OAuthToken.id)
    )).scalars().all()
    for r in rows:
        try:
            token = client._crypto.decrypt(r.access_token_enc)
        except Exception as e:
            skipped.append({"provider_code": r.provider_code, "owner": r.owner,
                            "reason": f"凭据解密失败：{type(e).__name__}"})
            continue
        targets.append(GrowthTarget(r.provider_code, r.owner, token))
    return targets, skipped


async def run_growth_batch(targets: List[GrowthTarget], *, trigger: str = "manual",
                           db=None, do_travel: bool = True, do_tasks: bool = True,
                           notify: bool = True) -> List[dict]:
    """顺序执行一批成长中心处理（旅行 + 任务），逐个落 growth_logs。

    **严格串行**：与签到同口径（上游对单账号高频请求敏感）。
    单账号失败不中断整批；每个动作各自按天幂等。
    """
    import json as _json
    results: List[dict] = []
    failures: List[str] = []
    gained_credit = 0.0
    for t in targets:
        for action, enabled, runner in (
            ("travel", do_travel, _run_travel_action),
            ("task", do_tasks, _run_task_action),
        ):
            if not enabled:
                continue
            if db is not None and await _already_done_today(db, t.provider_code, t.owner, action):
                results.append({"provider_code": t.provider_code, "owner": t.owner,
                                "action": action, "kind": "already",
                                "message": "今天已处理过，跳过"})
                continue
            t0 = time.monotonic()
            try:
                act = await runner(t.token, t.provider_code)
            except Exception as e:
                act = GrowthAction(kind="failed", message="执行异常",
                                   error=f"{type(e).__name__}: {str(e)[:200]}")
            duration_ms = int((time.monotonic() - t0) * 1000)
            rec = {
                "provider_code": t.provider_code, "owner": t.owner, "action": action,
                "kind": act.kind,
                "credit": act.credit if act.credit else None,
                "energy": act.energy if act.energy else None,
                "location": str(act.detail.get("location") or "")[:100] if act.detail else "",
                "task_codes": (_json.dumps(act.detail.get("accepted"), ensure_ascii=False)
                               if act.detail and act.detail.get("accepted") else None),
                "message": act.message, "error": act.error or None,
                "duration_ms": duration_ms,
            }
            results.append(rec)
            if act.kind == "failed":
                failures.append(f"{t.provider_code}/{t.owner}[{action}]: {act.message}"
                                + (f"（{act.error}）" if act.error else ""))
            if act.credit:
                gained_credit += act.credit
            logger.info("growth %s/%s %s -> %s credit=%s (%dms)",
                        t.provider_code, t.owner, action, act.kind, act.credit, duration_ms)
            if db is not None:
                try:
                    from server.models.growth_log import GrowthLog
                    db.add(GrowthLog(trigger=trigger, **rec))
                    await db.commit()
                except Exception as e:
                    logger.warning("growth log write failed: %s", e)
                    try:
                        await db.rollback()
                    except Exception:
                        pass
    if notify and failures:
        try:
            from server.core.notifier import notify_event
            notify_event("checkin", f"成长中心处理失败 {len(failures)} 项：" + "；".join(failures[:5]))
        except Exception:
            pass
    return results


async def _run_travel_action(token: str, provider_code: str) -> GrowthAction:
    return await run_travel(token, provider_code)


async def _run_task_action(token: str, provider_code: str) -> GrowthAction:
    return await accept_automatable_tasks(token, provider_code)


async def fetch_growth_overview(db) -> List[dict]:
    """所有 CodeBuddy 账号的成长中心只读快照（供 UI 展示，**不写任何东西**）。

    严格串行 + 失败隔离：单个账号拿不到就如实标不可用，不影响其它账号。
    """
    targets, skipped = await collect_growth_targets(db)
    out: List[dict] = []
    for t in targets:
        try:
            snap = await fetch_snapshot(t.token, t.provider_code)
            rec = {"provider_code": t.provider_code, "owner": t.owner}
            rec.update(snap.to_dict())
        except Exception as e:
            rec = {"provider_code": t.provider_code, "owner": t.owner, "ok": False,
                   "message": f"{type(e).__name__}: {str(e)[:150]}"}
        out.append(rec)
    for s in skipped:
        out.append({"provider_code": s["provider_code"], "owner": s["owner"],
                    "ok": False, "message": s["reason"]})
    return out
