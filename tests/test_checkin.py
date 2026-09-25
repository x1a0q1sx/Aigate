# -*- coding: utf-8 -*-
"""每日签到（领取上游免费积分/额度）。

协议来自 Jet-Hub 源码逐行核对 + 本机生产实测（2026-09-24），见 core/checkin.py
模块 docstring。本文件锁死的都是「协议反直觉之处」——它们是实现正确性的关键：

- CodeBuddy 幂等判据是 **body code**（10001/1001）而非 HTTP 状态
  （重复领取返回 HTTP 400 + code 10001）
- CodeBuddy 401/403 可能返回 **HTML 而非 JSON**（必须不炸、且能报「凭据失效」）
- Qoder 幂等判据是 **body.replayed**（重复领取同样返回 HTTP 200 且无 benefit）
  ——只看状态码会把「今天已领」误报成「+100 积分」
- Qoder 的 claim body 必须是**空串**而非 {}
- Qoder 只领 CLAIM_BENEFIT + CLAIMABLE（VIEW_DETAILS 型发了是错的）
- 状态查询用 checkin-activity-status（checkin-status 是占位数据）
- 已签到时**不发 claim 请求**（省调用 + 避免风控）
- **严格串行**（并发易触发上游风控）
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.models.base import Base
from server.models.checkin_log import CheckinLog
from server.models.oauth_token import OAuthToken


# ── httpx 假客户端（照 tests/test_oauth_usage.py 的范式）────────

def make_httpx(calls, results):
    """results: [(status, json_data_or_None, raw_text_or_None)]，按调用顺序消费。"""
    results = list(results)

    class _Resp:
        def __init__(self, status, data, raw):
            self.status_code = status
            self._data = data if data is not None else {}
            self._raw = raw
            self.content = (raw if raw is not None else json.dumps(self._data)).encode()
            self.text = raw if raw is not None else json.dumps(self._data)

        @property
        def is_success(self):
            return 200 <= self.status_code < 400

        def json(self):
            if self._raw is not None:
                raise ValueError("not json")
            return self._data

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _next(self):
            return _Resp(*results.pop(0)) if results else _Resp(200, {}, None)

        async def get(self, url, headers=None, params=None):
            calls.append({"m": "GET", "url": url, "headers": headers or {}})
            return self._next()

        async def post(self, url, headers=None, json=None, content=None):
            calls.append({"m": "POST", "url": url, "headers": headers or {},
                          "json": json, "content": content})
            return self._next()

    return _Client


def _patch(monkeypatch, calls, results):
    from server.core import checkin as ck
    monkeypatch.setattr(ck.httpx, "AsyncClient", make_httpx(calls, results))


# ── CodeBuddy ────────────────────────────────────────────────

CB_HEADERS = {"X-Domain": "copilot.tencent.com", "X-Product": "SaaS",
              "X-Product-Code": "codebuddy"}


def _cb_status(today_checked=False, active=True, streak=0, total=0, activity=""):
    return (200, {"code": 0, "msg": "OK", "data": {
        "active": active, "today_checked_in": today_checked,
        "streak_days": streak, "daily_credit": 100, "total_credits": total,
        "activity_name": activity, "checkin_dates": []}}, None)


def test_codebuddy_claim_success(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _cb_status(today_checked=False, streak=5, total=500, activity="高校新生攻略"),
        (200, {"code": 0, "msg": "OK",
               "data": {"credit": 100, "streak_days": 6, "is_streak_day": True}}, None),
    ])
    out = asyncio.run(claim_for_provider("codebuddy_cn", "tok"))
    assert out.kind == "claimed"
    assert out.credit == 100
    assert out.streak_days == 6
    assert out.activity_name == "高校新生攻略"
    assert len(calls) == 2
    # 状态查询必须先于领取
    assert calls[0]["url"].endswith("/v2/billing/meter/checkin-activity-status")
    assert calls[1]["url"].endswith("/v2/billing/meter/daily-checkin")
    # X-Domain 必须跟产品配置走（不能跟凭据里的 domain）
    assert calls[0]["headers"]["X-Domain"] == "copilot.tencent.com"
    assert calls[0]["headers"]["X-Product-Code"] == "codebuddy"
    # 已确认非必需的头不能发（AIGate 也没存 user_id）
    assert "X-Device-Token" not in calls[0]["headers"]
    assert "X-User-Id" not in calls[0]["headers"]


def test_codebuddy_already_claimed_by_body_code(monkeypatch):
    """幂等判据是 body code 10001，且重复领取返回的是 HTTP 400 —— 不能只看状态码。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _cb_status(today_checked=False),
        (400, {"code": 10001, "msg": "今天已签到，请明天再来"}, None),
    ])
    out = asyncio.run(claim_for_provider("codebuddy_cn", "tok"))
    assert out.kind == "already_claimed"
    assert out.upstream_code == 10001
    assert "今天已签到" in out.message
    assert out.credit == 0.0        # 不得把「已领」当成得分


def test_codebuddy_already_claimed_alt_code_1001(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _cb_status(today_checked=False),
        (200, {"code": 1001, "msg": "已领取"}, None),
    ])
    out = asyncio.run(claim_for_provider("codebuddy_cn", "tok"))
    assert out.kind == "already_claimed"


def test_codebuddy_status_says_checked_skips_claim(monkeypatch):
    """状态端点已显示今日已签 → 不得再发 claim 请求（省调用 + 避免风控）。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [_cb_status(today_checked=True, streak=6, total=600)])
    out = asyncio.run(claim_for_provider("codebuddy_cn", "tok"))
    assert out.kind == "already_claimed"
    assert out.streak_days == 6
    assert out.total_credits == 600
    assert len(calls) == 1          # 只有状态查询，没有 claim


def test_codebuddy_inactive_when_activity_not_active(monkeypatch):
    """实测 codebuddy_intl 就是这种状态：端点通但活动未开启。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [_cb_status(today_checked=False, active=False,
                                           activity="本期：专家能量包")])
    out = asyncio.run(claim_for_provider("codebuddy_intl", "tok"))
    assert out.kind == "inactive"
    assert out.activity_name == "本期：专家能量包"
    assert len(calls) == 1


def test_codebuddy_no_qualification_and_ended(monkeypatch):
    from server.core.checkin import claim_for_provider
    for code in (1002, 1003):
        calls = []
        _patch(monkeypatch, calls, [
            _cb_status(today_checked=False),
            (200, {"code": code, "msg": "无资格" if code == 1002 else "活动结束"}, None),
        ])
        out = asyncio.run(claim_for_provider("codebuddy_cn", "tok"))
        assert out.kind == "inactive", f"code={code}"


def test_codebuddy_html_error_response_does_not_crash(monkeypatch):
    """401/403 时上游可能返回 HTML（不是 JSON）——必须不炸且能报「凭据失效」。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        (401, None, "<html><body>Unauthorized</body></html>"),
    ])
    out = asyncio.run(claim_for_provider("codebuddy_cn", "tok"))
    assert out.kind == "failed"
    assert "重新登录" in out.message
    assert "401" in out.error


def test_codebuddy_claim_html_error_after_ok_status(monkeypatch):
    """状态查询通过、claim 阶段凭据失效（同样可能是 HTML）。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _cb_status(today_checked=False),
        (403, None, "<html>Forbidden</html>"),
    ])
    out = asyncio.run(claim_for_provider("codebuddy_cn", "tok"))
    assert out.kind == "failed"
    assert "重新登录" in out.message


def test_codebuddy_intl_uses_own_host(monkeypatch):
    """国际版必须打 www.codebuddy.ai，且 X-Domain 一致。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [_cb_status(today_checked=True)])
    asyncio.run(claim_for_provider("codebuddy_intl", "tok"))
    assert "www.codebuddy.ai" in calls[0]["url"]
    assert calls[0]["headers"]["X-Domain"] == "www.codebuddy.ai"


def test_codebuddy_network_exception_is_failed(monkeypatch):
    from server.core import checkin as ck

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("connection reset")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ck.httpx, "AsyncClient", _Boom)
    out = asyncio.run(ck.claim_for_provider("codebuddy_cn", "tok"))
    assert out.kind == "failed"
    assert "RuntimeError" in out.error


# ── Qoder ────────────────────────────────────────────────────

def _q_campaigns(campaigns):
    return (200, {"uid": "u1", "showCampaign": bool(campaigns),
                  "claimable": bool(campaigns), "campaigns": campaigns}, None)


def _claimable(cid="c1", amount=100):
    return {"campaignId": cid, "campaignKey": "act-x", "actionType": "CLAIM_BENEFIT",
            "claimStatus": "CLAIMABLE", "benefit": {"kind": "CREDITS", "amount": amount}}


def test_qoder_claim_success(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _q_campaigns([_claimable()]),
        (200, {"grantId": "g1", "status": "CLAIMED", "replayed": False,
               "benefit": {"kind": "CREDITS", "amount": 100}}, None),
    ])
    out = asyncio.run(claim_for_provider("qoder", "dt-tok"))
    assert out.kind == "claimed"
    assert out.credit == 100
    # 头：4 个，状态查询不带 Content-Type
    h = calls[0]["headers"]
    assert h["Cosy-ClientType"] == "5"
    assert h["User-Agent"] == "Qoder"
    assert "Content-Type" not in h
    # claim 多一个 Content-Type，且 body 必须是空串（不是 {}）
    assert calls[1]["headers"]["Content-Type"] == "application/json"
    assert calls[1]["content"] == b""
    assert calls[1]["url"].endswith("/campaigns/c1/claim")


def test_qoder_replayed_is_already_claimed_not_credit(monkeypatch):
    """重复领取返回 HTTP 200 + replayed=true 且无 benefit —— 只看状态码会误报 +100。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _q_campaigns([_claimable()]),
        (200, {"grantId": "g0", "status": "CLAIMED", "replayed": True,
               "claimedAt": "2026-09-18T15:14:28Z"}, None),
    ])
    out = asyncio.run(claim_for_provider("qoder", "dt-tok"))
    assert out.kind == "already_claimed"
    assert out.credit == 0.0        # 关键：不得把旧记录当成新领取


def test_qoder_empty_campaigns_is_already_claimed(monkeypatch):
    """服务端在「今天已领」时返回空列表 —— 保守判已领，不判「无活动」。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        (200, {"showCampaign": False, "claimable": False, "campaigns": []}, None),
    ])
    out = asyncio.run(claim_for_provider("qoder", "dt-tok"))
    assert out.kind == "already_claimed"
    assert len(calls) == 1          # 无可领活动不发 claim


def test_qoder_skips_view_details_campaigns(monkeypatch):
    """实测还有 VIEW_DETAILS 型活动（如「Pro 首月翻倍」），对它发 claim 是错的。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _q_campaigns([
            {"campaignId": "c-details", "actionType": "VIEW_DETAILS",
             "claimStatus": "CLAIMABLE", "benefit": {"amount": 999}},
        ]),
    ])
    out = asyncio.run(claim_for_provider("qoder", "dt-tok"))
    assert out.kind == "already_claimed"    # 无可领 → 已领语义
    assert len(calls) == 1                  # 没有对 VIEW_DETAILS 发 claim


def test_qoder_skips_already_claimed_campaigns(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _q_campaigns([
            {"campaignId": "c-old", "actionType": "CLAIM_BENEFIT",
             "claimStatus": "CLAIMED", "benefit": {"amount": 100}},
        ]),
    ])
    out = asyncio.run(claim_for_provider("qoder", "dt-tok"))
    assert out.kind == "already_claimed"
    assert len(calls) == 1


def test_qoder_multiple_campaigns_accumulate(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _q_campaigns([_claimable("c1", 100), _claimable("c2", 50)]),
        (200, {"status": "CLAIMED", "replayed": False, "benefit": {"amount": 100}}, None),
        (200, {"status": "CLAIMED", "replayed": False, "benefit": {"amount": 50}}, None),
    ])
    out = asyncio.run(claim_for_provider("qoder", "dt-tok"))
    assert out.kind == "claimed"
    assert out.credit == 150        # 多活动累加


def test_qoder_status_not_claimed_is_failed(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _q_campaigns([_claimable()]),
        (200, {"status": "PENDING", "replayed": False}, None),
    ])
    out = asyncio.run(claim_for_provider("qoder", "dt-tok"))
    assert out.kind == "failed"
    assert "PENDING" in out.error


def test_qoder_401_reports_reauth(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [(401, None, '{"error":"unauthorized"}')])
    out = asyncio.run(claim_for_provider("qoder", "dt-tok"))
    assert out.kind == "failed"
    assert "重新登录" in out.message


# ── 能力矩阵 / 分发 ──────────────────────────────────────────

def test_u1s1_marked_unsupported_without_http(monkeypatch):
    """u1s1 实测无签到接口（8 个候选端点全 404）→ 不发任何请求。"""
    from server.core.checkin import claim_for_provider, CHECKIN_CAPABILITIES
    assert CHECKIN_CAPABILITIES["u1s1"] is False
    calls = []
    _patch(monkeypatch, calls, [])
    out = asyncio.run(claim_for_provider("u1s1", "tok"))
    assert out.kind == "unsupported"
    assert calls == []


def test_unknown_provider_is_unsupported(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [])
    out = asyncio.run(claim_for_provider("claude_code", "tok"))
    assert out.kind == "unsupported"


def test_capabilities_match_probed_evidence():
    """能力矩阵必须与实测一致（防有人凭猜改回去）。"""
    from server.core.checkin import CHECKIN_CAPABILITIES
    assert CHECKIN_CAPABILITIES["codebuddy_cn"] is True     # 实测 200 且已签到
    assert CHECKIN_CAPABILITIES["codebuddy_intl"] is True   # 端点实测存在（活动未开）
    assert CHECKIN_CAPABILITIES["qoder"] is True            # 实测 200
    assert CHECKIN_CAPABILITIES["u1s1"] is False            # 实测 8 端点全 404


# ── 批量执行：串行 + 落库 + 通知 ─────────────────────────────

async def _db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/ck.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[CheckinLog.__table__, OAuthToken.__table__])
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_batch_runs_serially(tmp_path, monkeypatch):
    """**严格串行** —— 并发易触发上游风控（Jet-Hub 单测锁死 maxInFlight==1）。"""
    from server.core import checkin as ck
    engine, Session = await _db(tmp_path)
    try:
        inflight = 0
        max_inflight = 0

        async def fake_claim(code, token):
            nonlocal inflight, max_inflight
            inflight += 1
            max_inflight = max(max_inflight, inflight)
            await asyncio.sleep(0.01)
            inflight -= 1
            return ck.ClaimOutcome(kind="claimed", credit=100)

        monkeypatch.setattr(ck, "claim_for_provider", fake_claim)
        targets = [ck.CheckinTarget("codebuddy_cn", f"acct-{i}", "tok") for i in range(5)]
        async with Session() as db:
            results = await ck.run_checkin_batch(targets, trigger="manual", db=db,
                                                 notify=False)
        assert len(results) == 5
        assert max_inflight == 1, f"并发度 {max_inflight} —— 必须串行"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_batch_persists_logs_and_continues_on_failure(tmp_path, monkeypatch):
    """单账号失败不中断整批，且每账号都落一行。"""
    from server.core import checkin as ck
    engine, Session = await _db(tmp_path)
    try:
        async def fake_claim(code, token):
            if code == "qoder":
                return ck.ClaimOutcome(kind="failed", message="boom", error="E1")
            return ck.ClaimOutcome(kind="claimed", credit=100, streak_days=3)

        monkeypatch.setattr(ck, "claim_for_provider", fake_claim)
        targets = [
            ck.CheckinTarget("codebuddy_cn", "a", "t"),
            ck.CheckinTarget("qoder", "b", "t"),
            ck.CheckinTarget("codebuddy_intl", "c", "t"),
        ]
        async with Session() as db:
            results = await ck.run_checkin_batch(targets, trigger="scheduled", db=db,
                                                 notify=False)
            rows = (await db.execute(select(CheckinLog))).scalars().all()
        assert len(results) == 3 and len(rows) == 3
        kinds = {r.provider_code: r.kind for r in rows}
        assert kinds == {"codebuddy_cn": "claimed", "qoder": "failed",
                         "codebuddy_intl": "claimed"}
        # 失败行必须记 error；成功行 credit 落库、已领行 credit 为 NULL
        q = next(r for r in rows if r.provider_code == "qoder")
        assert q.error == "E1" and q.trigger == "scheduled"
        cb = next(r for r in rows if r.provider_code == "codebuddy_cn")
        assert cb.credit == 100 and cb.streak_days == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_batch_already_claimed_stores_null_credit(tmp_path, monkeypatch):
    """「今天已领」落库时 credit 必须为 NULL（不是 0）——UI 据此区分。"""
    from server.core import checkin as ck
    engine, Session = await _db(tmp_path)
    try:
        async def fake_claim(code, token):
            return ck.ClaimOutcome(kind="already_claimed", message="今天已签到")

        monkeypatch.setattr(ck, "claim_for_provider", fake_claim)
        async with Session() as db:
            await ck.run_checkin_batch(
                [ck.CheckinTarget("codebuddy_cn", "a", "t")],
                trigger="manual", db=db, notify=False)
            row = (await db.execute(select(CheckinLog))).scalars().one()
        assert row.kind == "already_claimed"
        assert row.credit is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_batch_notifies_on_failure_only(tmp_path, monkeypatch):
    from server.core import checkin as ck
    from server.core import notifier
    engine, Session = await _db(tmp_path)
    try:
        sent = []
        monkeypatch.setattr(notifier, "notify_event",
                            lambda ev, txt, **kw: sent.append((ev, txt)))

        async def fake_claim(code, token):
            return ck.ClaimOutcome(kind="failed", message="boom")

        monkeypatch.setattr(ck, "claim_for_provider", fake_claim)
        async with Session() as db:
            await ck.run_checkin_batch([ck.CheckinTarget("qoder", "a", "t")],
                                       trigger="scheduled", db=db, notify=True)
        assert len(sent) == 1
        assert sent[0][0] == "checkin"
        assert "签到失败" in sent[0][1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_batch_no_notify_when_all_ok(tmp_path, monkeypatch):
    from server.core import checkin as ck
    from server.core import notifier
    engine, Session = await _db(tmp_path)
    try:
        sent = []
        monkeypatch.setattr(notifier, "notify_event",
                            lambda ev, txt, **kw: sent.append(ev))

        async def fake_claim(code, token):
            return ck.ClaimOutcome(kind="claimed", credit=100)

        monkeypatch.setattr(ck, "claim_for_provider", fake_claim)
        async with Session() as db:
            await ck.run_checkin_batch([ck.CheckinTarget("qoder", "a", "t")],
                                       trigger="scheduled", db=db, notify=True)
        assert sent == []
    finally:
        await engine.dispose()


# ── 幂等：今日已完成则跳过 ───────────────────────────────────

@pytest.mark.asyncio
async def test_already_done_today_true_after_success(tmp_path):
    from server.core.checkin import already_done_today
    engine, Session = await _db(tmp_path)
    try:
        async with Session() as db:
            db.add(CheckinLog(provider_code="codebuddy_cn", owner="a",
                              kind="claimed", credit=100))
            await db.commit()
            assert await already_done_today(db, "codebuddy_cn", "a") is True
            # 不同账号不受影响
            assert await already_done_today(db, "codebuddy_cn", "b") is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_already_done_today_ignores_failures_and_stale(tmp_path):
    """失败不算完成（下次仍会重试）；昨天的不算今天。"""
    from server.core.checkin import already_done_today
    engine, Session = await _db(tmp_path)
    try:
        async with Session() as db:
            db.add(CheckinLog(provider_code="qoder", owner="a", kind="failed",
                              message="boom"))
            # 昨天的成功记录（北京时间今天零点之前）
            db.add(CheckinLog(provider_code="qoder", owner="b", kind="claimed",
                              credit=100,
                              created_at=datetime.utcnow() - timedelta(days=2)))
            await db.commit()
            assert await already_done_today(db, "qoder", "a") is False
            assert await already_done_today(db, "qoder", "b") is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inactive_counts_as_attempted_today(tmp_path):
    """「活动未开启」当天不再重试。

    ⚠️ 生产回归（2026-09-24）：codebuddy_intl 实测 active=False，此前 inactive
    不算完成 → 进程崩溃循环重启时每次补签都打一遍上游，一次刷出 25 条重复日志。
    自动触发只在计划时刻之后发生，那时活动早已刷新完，仍报未开启即当天真没活动。
    """
    from server.core.checkin import already_done_today
    engine, Session = await _db(tmp_path)
    try:
        async with Session() as db:
            db.add(CheckinLog(provider_code="codebuddy_intl", owner="__default",
                              kind="inactive", message="签到活动未开启"))
            await db.commit()
            assert await already_done_today(db, "codebuddy_intl", "__default") is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collect_targets_skips_inactive_account(tmp_path, monkeypatch):
    """端到端：已判 inactive 的账号不得再出现在待签列表里。"""
    from server.core.checkin import collect_targets
    engine, Session = await _db(tmp_path)
    try:
        class _FakeCrypto:
            def decrypt(self, s):
                return s[4:] if s.startswith("enc-") else s

        async with Session() as db:
            db.add(OAuthToken(provider_code="codebuddy_intl", owner="__default",
                              access_token_enc="enc-tok", is_active=True))
            db.add(CheckinLog(provider_code="codebuddy_intl", owner="__default",
                              kind="inactive", message="活动未开启"))
            await db.commit()

            from server.core.oauth_client import get_oauth_client
            c = get_oauth_client()
            orig = c._crypto
            c._crypto = _FakeCrypto()
            try:
                targets, skipped = await collect_targets(db)
            finally:
                c._crypto = orig

        assert targets == []
        assert skipped[0]["reason"] == "already_done_today"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collect_targets_skips_unsupported_and_done(tmp_path):
    from server.core.checkin import collect_targets
    from server.core import checkin as ck
    engine, Session = await _db(tmp_path)
    try:
        class _FakeCrypto:
            def decrypt(self, s):
                return s[4:] if s.startswith("enc-") else s

        async with Session() as db:
            db.add_all([
                OAuthToken(provider_code="codebuddy_cn", owner="a",
                           access_token_enc="enc-tok-a", is_active=True),
                OAuthToken(provider_code="u1s1", owner="b",
                           access_token_enc="enc-tok-b", is_active=True),
                OAuthToken(provider_code="qoder", owner="c",
                           access_token_enc="enc-tok-c", is_active=True),
                OAuthToken(provider_code="codebuddy_cn", owner="dead",
                           access_token_enc="enc-x", is_active=False),
            ])
            await db.commit()
            # qoder 今天已完成
            db.add(CheckinLog(provider_code="qoder", owner="c", kind="claimed", credit=100))
            await db.commit()

            from server.core.oauth_client import get_oauth_client
            monkeypatch_ok = get_oauth_client()
            orig = monkeypatch_ok._crypto
            monkeypatch_ok._crypto = _FakeCrypto()
            try:
                targets, skipped = await collect_targets(db)
            finally:
                monkeypatch_ok._crypto = orig

        got = {(t.provider_code, t.owner) for t in targets}
        assert got == {("codebuddy_cn", "a")}          # 只有它可签
        reasons = {(s["provider_code"], s["owner"]): s["reason"] for s in skipped}
        assert reasons[("u1s1", "b")] == "unsupported"
        assert reasons[("qoder", "c")] == "already_done_today"
        assert ("codebuddy_cn", "dead") not in reasons  # 停用连接根本不查
    finally:
        await engine.dispose()


# ── 配置 / 端点 / 调度器守卫 ─────────────────────────────────

def test_config_defaults_and_registration():
    from server.config import Config, CheckinConfig
    c = Config()
    assert isinstance(c.checkin, CheckinConfig)
    # 用户选择：自动签到默认开
    assert c.checkin.enabled is True
    assert c.checkin.startup_catchup is True
    assert 0 <= c.checkin.hour <= 23 and 0 <= c.checkin.minute <= 59


def test_notify_gate_includes_checkin():
    import inspect
    from server.core import notifier
    src = inspect.getsource(notifier._enabled_for)
    assert '"checkin"' in src


def test_endpoints_registered():
    from server.api import checkin_router as cr
    paths = {r.path for r in cr.router.routes}
    for suffix in ("/checkin/overview", "/checkin/run", "/checkin/logs",
                   "/checkin/config"):
        assert any(p.endswith(suffix) for p in paths), f"缺 {suffix}: {paths}"


def test_router_prefix_is_admin_api():
    """prefix 必须是 /admin/api —— 否则未登录时返回 200+index.html 而非 401 JSON。"""
    from server.api import checkin_router as cr
    assert cr.router.prefix == "/admin/api"


def test_config_endpoint_clamps():
    import inspect
    from server.api import checkin_router as cr
    src = inspect.getsource(cr.put_checkin_config)
    assert "max(0, min(23" in src      # hour
    assert "max(0, min(59" in src      # minute


def test_router_registered_in_app():
    import inspect
    import server.main as m
    src = inspect.getsource(m)
    assert "checkin_router" in src
    assert "checkin_router)" in src


def test_scheduler_uses_beijing_timezone():
    """定时任务必须显式用 Asia/Shanghai —— 上游按 UTC+8 刷新，不能依赖服务器 TZ。"""
    import inspect
    import server.main as m
    src = inspect.getsource(m._register_checkin_job)
    assert 'timezone="Asia/Shanghai"' in src
    assert 'id="checkin_daily"' in src


def test_scheduler_has_reentry_guard():
    """签到可能跨分钟（多账号串行），必须有重入保护。"""
    import inspect
    import server.main as m
    src = inspect.getsource(m._run_checkin_daily)
    assert "_busy" in src


def test_startup_catchup_checks_time_and_config():
    import inspect
    import server.main as m
    src = inspect.getsource(m._checkin_startup_catchup)
    assert "startup_catchup" in src
    assert "now < target" in src       # 未到点则不补签


def test_checkin_module_documents_serial_requirement():
    """源码守卫：串行约束必须留在模块里（防有人改成并发触发风控）。"""
    import inspect
    from server.core import checkin
    src = inspect.getsource(checkin)
    assert "严格串行" in src
    assert "并发易触发" in src


def test_migration_and_model_registered():
    import inspect
    import server.db as sdb
    src = inspect.getsource(sdb)
    assert "CheckinLog" in src
    assert "idx_checkin_prov_owner_time" in src


# ── LobsterAI（有道龙虾）三步签到 ─────────────────────
@pytest.fixture(autouse=True)
def _seed_lobsterai_version():
    """预置客户端版本缓存。

    该测试文件的 httpx patch 是**模块级**的（两者 import 同一个 httpx 模块），
    不预置的话 `resolve_client_version` 会先消费掉第一个假响应。
    （同时也让测试不发真实网络请求。）
    """
    import time as _t
    from server.core import lobsterai as _lb
    saved = _lb._VERSION_CACHE
    _lb._VERSION_CACHE = (_t.monotonic() + 3600, "2026.9.23")
    yield
    _lb._VERSION_CACHE = saved


def _lb_envelope(data):
    return (200, {"code": 0, "msg": "OK", "data": data}, None)


def _lb_slot(slot_state="available", code="act-1", rev=7):
    return _lb_envelope({"slotState": slot_state,
                         "activity": {"activityCode": code, "configRevision": rev}})


def _lb_context(claimed=False, actions=("check_in",)):
    return _lb_envelope({"state": {"claimedToday": claimed}, "actions": list(actions)})


def test_lobsterai_three_steps_and_credit_fallback(monkeypatch):
    """三步协议（slot → context → check_in）+ 积分三级回退 creditsGranted。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [
        _lb_slot(),
        _lb_context(),
        _lb_envelope({"result": {"creditsGranted": 100}}),
    ])
    out = asyncio.run(claim_for_provider("lobsterai", "tok"))
    assert out.kind == "claimed"
    assert out.credit == 100
    assert "/api/client-activities/slot" in calls[0]["url"]
    assert calls[1]["url"].endswith("/api/client-activities/act-1/context?configRevision=7")
    assert calls[2]["url"].endswith("/api/client-activities/act-1/actions/check_in")
    # 槽位的三个伪装参数（照抄 sigin.py:52-53 —— 跑在 Linux 上也发 win32）
    assert "placement=desktop_sidebar" in calls[0]["url"]
    assert "containerApiVersion=2" in calls[0]["url"]
    assert "platform=win32" in calls[0]["url"]
    # 客户端幂等键（服务端据此去重）
    body = json.loads(calls[2]["content"].decode())
    assert body["configRevision"] == 7
    assert body["idempotencyKey"]
    assert body["payload"] == {}
    # 不认 CodeBuddy 那套归属头
    assert "X-Domain" not in calls[0]["headers"]
    assert "X-Product" not in calls[0]["headers"]


def test_lobsterai_credit_reward_fallback(monkeypatch):
    """积分字段三级回退：creditsGranted → rewardCredits → credits。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [_lb_slot(), _lb_context(),
                                _lb_envelope({"result": {"rewardCredits": 55}})])
    out = asyncio.run(claim_for_provider("lobsterai", "tok"))
    assert out.kind == "claimed" and out.credit == 55


def test_lobsterai_claimed_today_skips_claim(monkeypatch):
    """claimedToday → already_claimed，且**不发 check_in 请求**（省调用+避风控）。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [_lb_slot(), _lb_context(claimed=True)])
    out = asyncio.run(claim_for_provider("lobsterai", "tok"))
    assert out.kind == "already_claimed"
    assert len(calls) == 2        # 第三步没发


def test_lobsterai_no_slot_is_inactive(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [_lb_slot(slot_state="unavailable")])
    out = asyncio.run(claim_for_provider("lobsterai", "tok"))
    assert out.kind == "inactive"
    assert len(calls) == 1


def test_lobsterai_actions_without_checkin_is_inactive(monkeypatch):
    """活动存在但 actions 不含 check_in → inactive（与「今天已领」区分）。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [_lb_slot(), _lb_context(actions=("view_details",))])
    out = asyncio.run(claim_for_provider("lobsterai", "tok"))
    assert out.kind == "inactive"
    assert len(calls) == 2


def test_lobsterai_401_reports_relogin(monkeypatch):
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [(401, None, "<html>unauthorized</html>")])
    out = asyncio.run(claim_for_provider("lobsterai", "tok"))
    assert out.kind == "failed"
    assert "重新登录" in out.message


def test_lobsterai_business_error_in_200(monkeypatch):
    """HTTP 200 也可能带业务错误（实测无凭据返回 code:-1「未登录」）。"""
    from server.core.checkin import claim_for_provider
    calls = []
    _patch(monkeypatch, calls, [(200, {"code": -1, "message": "未登录", "data": None}, None)])
    out = asyncio.run(claim_for_provider("lobsterai", "tok"))
    assert out.kind == "failed"
    assert "未登录" in out.message


def test_lobsterai_capability_registered():
    from server.core.checkin import CHECKIN_CAPABILITIES
    assert CHECKIN_CAPABILITIES.get("lobsterai") is True
