"""OAuth 额度/余额查询测试（端口自 9router usage handlers 的解析语义）。"""
import asyncio
import json

import pytest

import server.core.oauth_usage as ou
from server.core.oauth_usage import (
    _parse_reset, get_connection_usage, _claude_code_usage, _codex_usage,
    _github_usage, _codebuddy_usage, _qoder_usage, _u1s1_usage,
)


def make_httpx(calls, results):
    """results: [(status, json_data)]；提供 .ok/.content 与 GET/POST 记录。"""
    results = list(results)

    class _Resp:
        def __init__(self, status, data):
            self.status_code = status
            self._data = data if data is not None else {}
            self.content = json.dumps(self._data).encode()
            self.text = json.dumps(self._data)

        @property
        def is_success(self):
            return 200 <= self.status_code < 400

        def json(self):
            return self._data

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _next(self):
            return _Resp(*results.pop(0)) if results else _Resp(200, {})

        async def get(self, url, headers=None, params=None):
            calls.append({"m": "GET", "url": url, "headers": headers or {}})
            return self._next()

        async def post(self, url, headers=None, json=None, content=None):
            calls.append({"m": "POST", "url": url, "headers": headers or {},
                          "json": json, "content": content})
            return self._next()

    return _Client


def _patch(monkeypatch, calls, results):
    monkeypatch.setattr(ou.httpx, "AsyncClient", make_httpx(calls, results))


def _clear_caches():
    ou._CACHE.clear()
    ou._COOLDOWN.clear()
    ou._INFLIGHT.clear()


@pytest.fixture(autouse=True)
def clean_caches():
    _clear_caches()
    yield
    _clear_caches()


def test_parse_reset_forms():
    assert _parse_reset(1781594470000).startswith("2026-")   # ms epoch
    assert _parse_reset(1781594470) == _parse_reset(1781594470000)
    assert _parse_reset("2026-09-18T07:00:00Z").startswith("2026-09-18")
    assert _parse_reset(None) is None


# ── claude_code ─────────────────────────────────────
def test_claude_usage_windows(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "five_hour": {"utilization": 87, "resets_at": 1789000000},
        "seven_day": {"utilization": 12, "resets_at": 1790000000},
        "seven_day_sonnet": {"utilization": 40},
        "limits": [{"kind": "weekly_scoped", "percent": 55,
                    "scope": {"model": {"display_name": "Fable"}}}],
    })])
    r = asyncio.run(_claude_code_usage("tok"))
    assert r["plan"] == "Claude Code"
    assert r["quotas"]["session (5h)"]["used"] == 87
    assert r["quotas"]["weekly (7d)"]["remaining"] == 88
    assert r["quotas"]["weekly sonnet (7d)"]["used"] == 40
    assert r["quotas"]["weekly fable (7d)"]["used"] == 55
    assert calls[0]["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert calls[0]["headers"]["anthropic-beta"] == "oauth-2025-04-20"


def test_claude_429_sets_cooldown_and_message(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(429, {})])
    with pytest.raises(ou._RateLimited):
        asyncio.run(_claude_code_usage("tok429"))


def test_get_usage_caches_and_force(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {"five_hour": {"utilization": 1}})])
    r1 = asyncio.run(get_connection_usage("claude_code", "tokA"))
    r2 = asyncio.run(get_connection_usage("claude_code", "tokA"))
    assert len(calls) == 1          # 第二次命中缓存
    assert r2.get("cached") is True
    r3 = asyncio.run(get_connection_usage("claude_code", "tokA", force=True))
    assert len(calls) == 2          # force 穿透


# ── codex ───────────────────────────────────────────
def test_codex_usage_windows_and_credits(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {"used_percent": 30, "reset_at": 1789000000},
            "secondary_window": {"used_percent": 5, "reset_at": 1790000000},
            "limit_reached": False,
        },
        "additional_rate_limits": [
            {"limit_name": "code_review", "primary": {"percent_used": 90}},
            {"limit_name": "gpt-5.3-codex-spark", "primary": {"percent_used": 20}},
        ],
        "rate_limit_reset_credits": {"available_count": 2},
    })])
    r = asyncio.run(_codex_usage("tok"))
    assert r["plan"] == "plus"
    assert r["quotas"]["session"]["used"] == 30
    assert r["quotas"]["weekly"]["used"] == 5
    assert r["quotas"]["review_session"]["used"] == 90
    assert r["quotas"]["spark_session"]["used"] == 20
    assert r["extra"]["reset_credits_available"] == 2


# ── github ──────────────────────────────────────────
def test_github_usage_paid_snapshot(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "copilot_plan": "copilot-pro",
        "quota_reset_date": 1790000000000,
        "quota_snapshots": {
            "chat": {"entitlement": 300, "remaining": 120},
            "completions": {"entitlement": 0, "remaining": 0, "unlimited": True},
            "premium_interactions": {"entitlement": 300, "remaining": 2},
        },
    })])
    r = asyncio.run(_github_usage("gho_x"))
    assert r["plan"] == "copilot-pro"
    assert r["quotas"]["chat"]["used"] == 180
    assert r["quotas"]["completions"]["unlimited"] is True
    assert r["quotas"]["premium_interactions"]["remaining"] == 2
    assert calls[0]["headers"]["Authorization"].startswith("token ")


def test_github_usage_free_quotas(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "access_type_sku": "free",
        "monthly_quotas": {"chat": 50, "completions": 2000},
        "limited_user_quotas": {"chat": 10},
        "limited_user_reset_date": 1790000000000,
    })])
    r = asyncio.run(_github_usage("gho_y"))
    assert r["quotas"]["chat"]["used"] == 10
    assert r["quotas"]["completions"]["total"] == 2000


# ── codebuddy ───────────────────────────────────────
def test_codebuddy_refill_vs_bonus_packs(monkeypatch):
    calls = []
    day = 86400000
    now_ms = 1789000000000
    _patch(monkeypatch, calls, [(200, {"code": 0, "data": {"Response": {"Data": {
        "Accounts": [
            # 续包：cycle 结束远早于扣费期结束
            {"PackageName": "基础体验包", "CycleCapacityUsedPrecise": "6.54",
             "CycleCapacitySizePrecise": "500",
             "CycleStartTime": now_ms, "CycleEndTime": now_ms + 20 * day,
             "DeductionEndTime": now_ms + 200 * day},
            # 赠包：cycle 结束 == 到期
            {"PackageName": "活动赠送包", "CapacityUsedPrecise": "30",
             "CapacitySizePrecise": "100",
             "CycleStartTime": now_ms, "CycleEndTime": now_ms + 5 * day,
             "DeductionEndTime": now_ms + 5 * day},
        ]}}}})])
    r = asyncio.run(_codebuddy_usage(
        "tok", "https://copilot.tencent.com/v2/chat/completions"))
    assert r["plan"] == "基础体验包"
    monthly = r["quotas"]["Monthly"]
    assert monthly["used"] == pytest.approx(6.54)
    assert monthly["total"] == 500
    assert monthly["recurring"] is True
    bonus = r["quotas"]["Bonus Pack 1"]
    assert bonus["used"] == 30 and bonus["recurring"] is False
    # 请求形态：POST {} + CLI 方言头
    assert calls[0]["m"] == "POST"
    assert calls[0]["content"] == b"{}"
    assert calls[0]["url"] == "https://copilot.tencent.com/v2/billing/meter/get-user-resource"


def test_codebuddy_error_code_message(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {"code": 500, "msg": "internal"})])
    r = asyncio.run(_codebuddy_usage("tok", "https://copilot.tencent.com/v2/chat/completions"))
    assert "internal" in r["message"]


# ── qoder ───────────────────────────────────────────
def test_qoder_usage_user_org(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "userQuota": {"total": 600, "used": 200, "remaining": 400, "unit": "credits"},
        "orgResourcePackage": {"total": 100, "used": 10, "remaining": 90},
        "totalUsagePercentage": 30, "isQuotaExceeded": False,
        "expiresAt": 1789999999000,
    })])
    r = asyncio.run(_qoder_usage("dt-abc"))
    assert r["quotas"]["user"]["remaining"] == 400
    assert r["quotas"]["organization"]["used"] == 10
    assert r["extra"]["is_quota_exceeded"] is False


def test_qoder_pat_exchanges_first(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [
        (200, {"token": "jt-xyz"}),
        (200, {"userQuota": {"total": 600, "used": 1, "remaining": 599}}),
    ])
    r = asyncio.run(_qoder_usage("pt-secret"))
    assert calls[0]["url"].endswith("/api/v1/jobToken/exchange")
    assert calls[0]["json"] == {"personal_token": "pt-secret"}
    assert "jt-xyz" in calls[1]["headers"]["Authorization"]  # 额度查询用换出的 jt
    assert r["quotas"]["user"]["remaining"] == 599


# ── u1s1 ────────────────────────────────────────────
def test_u1s1_me_balance(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "email": "a@b.c", "remaining_usd": 3.25,
        "daily_free_usd": 1.0, "daily_free_remaining_usd": 0.4,
        "tokens_per_usd": 200000, "free_claim": "",
    })])
    r = asyncio.run(_u1s1_usage("u1s1-key"))
    perm = r["quotas"]["永久余额"]
    assert perm["total"] == pytest.approx(3.25)
    assert "650,000" in perm["display_name"]
    daily = r["quotas"]["今日免费"]
    assert daily["used"] == pytest.approx(0.6)
    assert calls[0]["url"] == "https://api.u1s1.io/v1/me"


def test_dispatch_unknown_provider_message():
    r = asyncio.run(get_connection_usage("kimchi", "tok"))
    assert "额度接口" in r["message"] or "没有" in r["message"]


# ── 额度按到期顺序排序（用户需求 2026-09-22） ─────────────────────
def test_sort_quotas_by_expiry():
    """最早到期的排最前；上游返回顺序无意义，需按 reset_at 升序。"""
    q = {
        "Bonus Pack 1": {"reset_at": "2026-10-05T00:00:00+00:00"},
        "Monthly":      {"reset_at": "2026-10-12T00:00:00+00:00"},
        "Bonus Pack 2": {"reset_at": "2026-09-25T00:00:00+00:00"},
    }
    assert list(ou.sort_quotas(q)) == ["Bonus Pack 2", "Bonus Pack 1", "Monthly"]


def test_sort_quotas_no_expiry_last():
    """无到期时间 / 不限量 / 9999 哨兵 一律排最后。"""
    q = {
        "永久余额": {"reset_at": None, "unit": "USD"},
        "今日免费": {"reset_at": "2026-09-23T00:00:00+00:00"},
        "不限量包": {"unlimited": True},
        "哨兵":     {"reset_at": "9999-12-31T00:00:00+00:00"},
    }
    assert list(ou.sort_quotas(q)) == ["今日免费", "不限量包", "哨兵", "永久余额"]

    q2 = {"b": {"reset_at": "2026-01-02T00:00:00+00:00"}, "a": {}}
    assert list(ou.sort_quotas(q2)) == ["b", "a"]


def test_sort_quotas_tie_break_and_degenerate():
    """同到期按名称稳定；空/单元素/None/坏值 不抛异常。"""
    same = {
        "organization": {"reset_at": "2026-10-01T00:00:00+00:00"},
        "user":         {"reset_at": "2026-10-01T00:00:00+00:00"},
    }
    assert list(ou.sort_quotas(same)) == ["organization", "user"]
    assert ou.sort_quotas({}) == {}
    assert ou.sort_quotas(None) == {}
    assert list(ou.sort_quotas({"a": {}})) == ["a"]
    bad = {"x": {"reset_at": "not-a-date"},
           "y": {"reset_at": "2026-01-01T00:00:00Z"}}
    assert list(ou.sort_quotas(bad)) == ["y", "x"]


def test_reset_key_handles_naive_and_zulu():
    """naive 时间按 UTC 解析；Z 结尾同样可用（_parse_reset 产出的就是这种）。"""
    a = ou._reset_key({"reset_at": "2026-05-01T00:00:00"})
    b = ou._reset_key({"reset_at": "2026-05-01T00:00:00+00:00"})
    c = ou._reset_key({"reset_at": "2026-05-01T00:00:00Z"})
    assert a == b == c
    assert a[0] == 0, "有到期时间的应归入第一组"


def test_get_connection_usage_sorts_quotas(monkeypatch):
    """端到端：上游乱序返回 → get_connection_usage 出口已排好序。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {"code": 0, "data": {"Response": {"Data": {
        "Accounts": [
            {"PackageName": "A", "CapacityUsedPrecise": "1", "CapacitySizePrecise": "10",
             "CycleStartTime": 1789000000000, "CycleEndTime": 1791500000000,
             "DeductionEndTime": 1791500000000},          # 赠包1：更早到期
            {"PackageName": "B", "CapacityUsedPrecise": "2", "CapacitySizePrecise": "20",
             "CycleStartTime": 1789000000000, "CycleEndTime": 1794000000000,
             "DeductionEndTime": 1794000000000},          # 赠包2：更晚到期
        ]}}}})])

    async def _no_cache():
        ou._CACHE.clear()
        ou._INFLIGHT.clear()
        ou._COOLDOWN.clear()
    asyncio.run(_no_cache())
    r = asyncio.run(get_connection_usage("codebuddy_cn", "tok-sort"))
    names = list(r["quotas"])
    assert names == ["Bonus Pack 1", "Bonus Pack 2"], "应按到期升序：%s" % names
    q1 = r["quotas"]["Bonus Pack 1"]["reset_at"]
    q2 = r["quotas"]["Bonus Pack 2"]["reset_at"]
    assert q1 < q2
