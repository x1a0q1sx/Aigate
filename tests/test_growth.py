# -*- coding: utf-8 -*-
"""CodeBuddy 成长中心（Buddy 旅行 / 成长任务）。

来源：WorkDaddy 调研（docs/findings-workdaddy-features.md），端点经生产实测。
本文件锁死的都是「不做就会出错」的点：

- **只做白名单内的任务**（照抄 WorkDaddy 的 AUTOMATABLE_TASK_CODES）——
  白名单外的任务自动化必失败，必须如实列出而非硬做
- **`instance_id` 必须纯数字**（上游要求；WorkDaddy 强制校验）—— 防字符串/数字混用
- **旅行状态机**：arrived → claim；idle 且未达上限 → depart；traveling → 不动
- **按天幂等**：同一动作当天只做一次（failed 不算，必须允许重试）
- **严格串行**：并发易触发上游风控
- 国际版数据为空时如实标「本版无此活动」，不报错
"""
import asyncio
import json

import pytest

from server.core import codebuddy_growth as cg


# ── httpx 假客户端（照 tests/test_checkin.py 的范式）────────────

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
                          "content": content})
            return self._next()

    return _Client


def _patch(monkeypatch, calls, results):
    monkeypatch.setattr(cg.httpx, "AsyncClient", make_httpx(calls, results))


# ── 响应构造 ─────────────────────────────────────────────────

def _ok(data):
    return (200, {"code": 0, "msg": "OK", "data": data}, None)


def _travel_status(state="idle", location=None, arrive=0, limit=False, reward=0):
    return _ok({"state": state, "buddy_id": 1, "record_id": 2,
                "location": location, "depart_at": 0, "arrive_at": arrive,
                "daily_limit_reached": limit, "reward_credit": reward})


def _buddy_info(name="星际喵", rarity="SR", iid=7895878):
    return _ok({"buddy": {"instance_id": iid, "name": name, "rarity": rarity}})


def _buddy_list(n=3):
    return _ok({"buddies": [{"instance_id": 1000 + i} for i in range(n)], "count": n})


def _tasks(items):
    return _ok({"tasks": items})


def _task(code, status="not_accepted", cur=0, tgt=1, credit=100, energy=5, title=""):
    return {"task_code": code, "title": title or code, "accept_status": status,
            "progress": {"current": cur, "target": tgt},
            "reward_credit": credit, "reward_energy": energy}


def _streak(days=3, tier="7d", remain=4, cards=1):
    return _ok({"streak": {"days": days, "next_tier": tier, "next_tier_remaining": remain},
                "makeup_cards": {"balance": cards, "max": 4},
                "redemption_status": {"tiers": [
                    {"tier": "7d", "days": 7, "credit": 0, "energy": 2, "cards": 1, "chances": 1}]}})


def _quota(affordable=1, balance=13, cost=10):
    return _ok({"affordable": affordable, "balance": balance, "cost_per_open": cost})


def _chances(n=0):
    return _ok({"balance": n})


def _snapshot_results(travel=None, info=None, blist=None, tasks=None, streak=None,
                      quota=None, chances=None):
    """按 fetch_snapshot 的调用顺序拼响应：travel, info, list, tasks, streak, quota, chances。"""
    return [travel or _travel_status(), info or _buddy_info(), blist or _buddy_list(),
            tasks or _tasks([]), streak or _streak(), quota or _quota(), chances or _chances()]


# ── 纯函数：credits 解析 / id 校验 / 任务码校验 ────────────────

def test_task_code_validation_rejects_injection():
    assert cg._task_code_ok("create_canvas") is True
    assert cg._task_code_ok("Model_chat_GLM5.2") is True
    for bad in ("", "a b", "a/b", "a;b", "'; DROP", "x" * 97, None, 123):
        assert cg._task_code_ok(bad) is False, f"{bad!r} 必须被拒"


def test_safe_int_id_requires_pure_digits():
    """上游要求 instance_id 是纯数字（WorkDaddy 强制校验）—— 防字符串/数字混用。"""
    assert cg._safe_int_id(7895878) == 7895878
    assert cg._safe_int_id("7895878") == 7895878
    assert cg._safe_int_id(" 7895878 ") == 7895878
    for bad in ("7895878abc", "abc", "", None, 0, -1, True, False, 1.5, []):
        assert cg._safe_int_id(bad) is None, f"{bad!r} 必须被拒"


def test_automatable_codes_match_workdaddy_whitelist():
    """白名单必须与 WorkDaddy 源码一致（照抄，不自行增删）。"""
    expected = {
        "create_canvas", "template_5", "expert_5", "Expert_team_use_3", "automation_1",
        "playbook_prompt", "Expert_lighthouse", "Buddy_App", "Buddy_App_QQ",
        "Hp_Appearance", "chat_5", "Model_chat_GLM5.2", "black_cat", "Library_read",
    }
    assert set(cg.AUTOMATABLE_TASK_CODES) == expected


# ── 只读快照 ─────────────────────────────────────────────────

def test_snapshot_parses_all_sections(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, _snapshot_results(
        travel=_travel_status(state="traveling", location={"name": "咖啡馆"},
                              arrive=1790588559, limit=False, reward=50),
        info=_buddy_info("星际喵", "SR", 7895878),
        blist=_buddy_list(7),
        tasks=_tasks([
            _task("create_canvas", "accepted", 0, 1, 300, 5),
            _task("RichMeow_Chat", "claimed", 1, 1, 100, 5),
            _task("Expert_Philanthropy", "not_accepted", 0, 1, 300, 0),
        ]),
        streak=_streak(3, "7d", 4, 1),
        quota=_quota(1, 13, 10),
        chances=_chances(2),
    ))
    s = asyncio.run(cg.fetch_snapshot("tok", "codebuddy_cn"))
    assert s.ok is True
    assert s.travel_state == "traveling"
    assert s.travel_location == "咖啡馆"
    assert s.travel_arrive_at == 1790588559
    assert s.buddy_name == "星际喵" and s.buddy_rarity == "SR"
    assert s.buddy_instance_id == 7895878 and s.buddy_count == 7
    assert s.task_total == 3 and s.task_completed == 1
    # 未领奖励 = 300 + 300（claimed 的不算）
    assert s.task_claimable == 600
    assert s.streak_days == 3 and s.makeup_cards == 1
    assert s.gacha_affordable == 1 and s.lottery_chances == 2
    # 白名单内 + 未完成 → 可自动；白名单外 → 需手动
    by_code = {t["code"]: t for t in s.tasks}
    assert by_code["create_canvas"]["automatable"] is True
    assert by_code["RichMeow_Chat"]["automatable"] is False   # 已完成
    assert by_code["Expert_Philanthropy"]["automatable"] is False  # 不在白名单
    assert s.manual_tasks == ["Expert_Philanthropy"]


def test_snapshot_intl_empty_is_ok_not_error(monkeypatch):
    """国际版数据为空（buddy:null / buddies:[] / travel 空）→ ok=True，如实显示。"""
    calls = []
    _patch(monkeypatch, calls, [
        _ok({}),                       # travel/status 空
        _ok({"buddy": None}),          # info
        _ok({"buddies": [], "count": 0}),
        _tasks([]),
        _ok({}),                       # streak 空
        _ok({}), _ok({}),
    ])
    s = asyncio.run(cg.fetch_snapshot("tok", "codebuddy_intl"))
    assert s.ok is True
    assert s.buddy_name == "" and s.buddy_count == 0
    assert s.travel_state == "unknown"
    assert s.tasks == []


def test_snapshot_all_fail_reports_message(monkeypatch):
    """全部失败 → ok=False + 原因（不得静默返回空快照）。"""
    calls = []
    _patch(monkeypatch, calls, [(500, {"code": 500, "msg": "boom"}, None)] * 10)
    s = asyncio.run(cg.fetch_snapshot("tok", "codebuddy_cn"))
    assert s.ok is False
    assert s.message


def test_snapshot_401_reports_relogin(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(401, None, "<html>unauthorized</html>")] * 10)
    s = asyncio.run(cg.fetch_snapshot("tok", "codebuddy_cn"))
    assert s.ok is False
    assert "重新登录" in s.message


def test_snapshot_unknown_provider():
    s = asyncio.run(cg.fetch_snapshot("tok", "qoder"))
    assert s.ok is False and "未知" in s.message


# ── 旅行：状态机 ─────────────────────────────────────────────

def test_run_travel_claims_when_arrived(monkeypatch):
    """arrived → claim（并带回 credit/energy）。"""
    calls = []
    _patch(monkeypatch, calls, _snapshot_results(
        travel=_travel_status(state="arrived", reward=50)) + [
        _ok({"reward_credit": 50, "reward_energy": 3})])
    act = asyncio.run(cg.run_travel("tok", "codebuddy_cn"))
    assert act.kind == "claimed" and act.credit == 50 and act.energy == 3
    assert calls[-1]["m"] == "POST" and "travel/claim" in calls[-1]["url"]


def test_run_travel_departs_when_idle(monkeypatch):
    """idle + 未达上限 + 有 Buddy → depart（取 config 第一个地点）。"""
    calls = []
    _patch(monkeypatch, calls, _snapshot_results(
        travel=_travel_status(state="idle")) + [
        _ok({"locations": [{"id": 1, "name": "咖啡馆"}, {"id": 2, "name": "商场"}]}),
        _ok({"ok": True}),
    ])
    act = asyncio.run(cg.run_travel("tok", "codebuddy_cn"))
    assert act.kind == "traveled"
    assert act.detail["location_id"] == 1
    posts = [c for c in calls if c["m"] == "POST"]
    assert len(posts) == 1 and "travel/depart" in posts[0]["url"]
    assert json.loads(posts[0]["content"]) == {"location_id": 1}


def test_run_travel_skips_when_traveling(monkeypatch):
    """traveling → 什么都不做（等到达，不重复派发）。"""
    calls = []
    _patch(monkeypatch, calls, _snapshot_results(
        travel=_travel_status(state="traveling", location={"name": "咖啡馆"})))
    act = asyncio.run(cg.run_travel("tok", "codebuddy_cn"))
    assert act.kind == "already"
    assert not [c for c in calls if c["m"] == "POST"], "不得发写请求"


def test_run_travel_skips_when_daily_limit(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, _snapshot_results(
        travel=_travel_status(state="idle", limit=True)))
    act = asyncio.run(cg.run_travel("tok", "codebuddy_cn"))
    assert act.kind == "already" and "用完" in act.message
    assert not [c for c in calls if c["m"] == "POST"]


def test_run_travel_skips_without_buddy(monkeypatch):
    """没有 Buddy（未领养）→ inactive，不硬派发。"""
    calls = []
    _patch(monkeypatch, calls, [
        _travel_status(state="idle"),
        _ok({"buddy": None}), _ok({"buddies": [], "count": 0}),
        _tasks([]), _streak(), _quota(), _chances(),
    ])
    act = asyncio.run(cg.run_travel("tok", "codebuddy_cn"))
    assert act.kind == "inactive" and "Buddy" in act.message
    assert not [c for c in calls if c["m"] == "POST"]


def test_run_travel_locked_reports_no_activity(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, _snapshot_results(
        travel=_travel_status(state="locked")))
    act = asyncio.run(cg.run_travel("tok", "codebuddy_intl"))
    assert act.kind == "inactive" and "本版无" in act.message


def test_travel_depart_no_locations_is_inactive(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [_ok({"locations": []})])
    act = asyncio.run(cg.travel_depart("tok", "codebuddy_intl"))
    assert act.kind == "inactive" and "暂无可用旅行地点" in act.message


def test_travel_depart_rejects_non_numeric_location(monkeypatch):
    """地点 id 非纯数字 → 拒绝（不把脏值发给上游）。"""
    calls = []
    _patch(monkeypatch, calls, [_ok({"locations": [{"id": "abc", "name": "x"}]})])
    act = asyncio.run(cg.travel_depart("tok", "codebuddy_cn"))
    assert act.kind == "failed" and "非法" in act.message
    assert not [c for c in calls if c["m"] == "POST"]


# ── 成长任务：白名单 ─────────────────────────────────────────

def test_accept_automatable_only_takes_whitelist(monkeypatch):
    """只接「白名单内 + 未接取」的任务 —— 白名单外的绝不自动接。"""
    calls = []
    _patch(monkeypatch, calls, _snapshot_results(
        tasks=_tasks([
            _task("create_canvas", "not_accepted"),        # 白名单 ✓
            _task("Library_read", "not_accepted"),         # 白名单 ✓
            _task("Expert_Philanthropy", "not_accepted"),  # 白名单外 ✗
            _task("chat_5", "claimed"),                    # 已完成 ✗
            _task("template_5", "accepted"),               # 已接取 ✗
        ])) + [_ok({"accepted": ["create_canvas", "Library_read"]})])
    act = asyncio.run(cg.accept_automatable_tasks("tok", "codebuddy_cn"))
    assert act.kind == "accepted"
    assert act.detail["accepted"] == ["create_canvas", "Library_read"]
    posts = [c for c in calls if c["m"] == "POST"]
    assert len(posts) == 1 and "tasks/accept" in posts[0]["url"]
    body = json.loads(posts[0]["content"])
    assert set(body["task_codes"]) == {"create_canvas", "Library_read"}


def test_accept_automatable_noop_when_nothing_pending(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, _snapshot_results(
        tasks=_tasks([_task("chat_5", "claimed"), _task("create_canvas", "accepted")])))
    act = asyncio.run(cg.accept_automatable_tasks("tok", "codebuddy_cn"))
    assert act.kind == "already"
    assert not [c for c in calls if c["m"] == "POST"]


def test_accept_tasks_rejects_bad_codes(monkeypatch):
    """非法任务码被过滤；全部非法 → 不发请求。"""
    calls = []
    _patch(monkeypatch, calls, [])
    act = asyncio.run(cg.accept_tasks("tok", "codebuddy_cn", ["a b", "'; DROP", ""]))
    assert act.kind == "inactive"
    assert not calls


def test_accept_tasks_dedupes_and_caps(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [_ok({})])
    codes = ["a"] * 5 + [f"t{i}" for i in range(30)]
    act = asyncio.run(cg.accept_tasks("tok", "codebuddy_cn", codes))
    assert act.kind == "accepted"
    body = json.loads([c for c in calls if c["m"] == "POST"][0]["content"])
    assert len(body["task_codes"]) <= 20
    assert len(set(body["task_codes"])) == len(body["task_codes"])


# ── 错误面 ───────────────────────────────────────────────────

def test_business_error_in_200_is_failed(monkeypatch):
    """HTTP 200 但 code != 0 → failed（不得当成功）。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {"code": 40001, "msg": "任务不存在"}, None)])
    act = asyncio.run(cg.accept_tasks("tok", "codebuddy_cn", ["create_canvas"]))
    assert act.kind == "failed" and "任务不存在" in act.message


def test_network_exception_is_failed(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise cg.httpx.ConnectError("boom")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cg.httpx, "AsyncClient", _Boom)
    act = asyncio.run(cg.travel_claim("tok", "codebuddy_cn"))
    assert act.kind == "failed" and "网络" in act.message or "ConnectError" in act.error


def test_unknown_provider_is_unsupported():
    act = asyncio.run(cg.travel_claim("tok", "qoder"))
    assert act.kind == "unsupported"


# ── 请求头（实测形态）────────────────────────────────────────

def test_headers_include_web_shape_and_codebuddy_standard():
    """WorkDaddy 实测用 web 形态（origin/referer/x-client-platform）；
    CodeBuddy 标准头（X-Domain 等）也要带 —— 生产实测两者兼容。"""
    h = cg.growth_headers("tok", cg.GROWTH_PRODUCTS["codebuddy_cn"])
    assert h["x-client-platform"] == "web"
    assert "/profile/growth-center" in h["referer"]
    assert h["X-Domain"] == "copilot.tencent.com"
    assert h["X-Product"] == "SaaS" and h["X-Product-Code"] == "codebuddy"
    assert h["Authorization"] == "Bearer tok"


def test_intl_headers_use_own_host():
    h = cg.growth_headers("tok", cg.GROWTH_PRODUCTS["codebuddy_intl"])
    assert h["X-Domain"] == "www.codebuddy.ai"
    assert "codebuddy.ai" in h["origin"]


# ── 只读性守卫 ───────────────────────────────────────────────

def test_snapshot_is_read_only(monkeypatch):
    """fetch_snapshot 只允许 GET —— 写操作必须走显式函数。"""
    calls = []
    _patch(monkeypatch, calls, _snapshot_results())
    asyncio.run(cg.fetch_snapshot("tok", "codebuddy_cn"))
    assert calls, "必须有请求"
    assert all(c["m"] == "GET" for c in calls), "快照不得发写请求"


def test_fetch_overview_never_writes(monkeypatch):
    """fetch_growth_overview（UI 用）同样只读。"""
    calls = []
    _patch(monkeypatch, calls, _snapshot_results() * 2)

    class _Row:
        provider_code = "codebuddy_cn"
        owner = "u1"
        access_token_enc = "enc-tok"   # collect_growth_targets 会解密该列

    class _DB:
        async def execute(self, *a, **k):
            class _R:
                def scalars(self):
                    return self

                def all(self):
                    return [_Row()]
            return _R()

    class _Crypto:
        @staticmethod
        def decrypt(x):
            return "tok"

    class _Client:
        # 与生产一致：OAuthClient 实例上有 _crypto（crypto_service.get_crypto_service()）
        _crypto = _Crypto()

    monkeypatch.setattr("server.core.oauth_client.get_oauth_client", lambda: _Client())
    out = asyncio.run(cg.fetch_growth_overview(_DB()))
    assert len(out) == 1 and out[0]["ok"] is True
    assert all(c["m"] == "GET" for c in calls)


# ── 批量执行：串行 + 按天幂等 ────────────────────────────────

def test_run_growth_batch_is_serial(monkeypatch):
    """严格串行（并发易触发上游风控）。"""
    inflight = {"n": 0, "max": 0}
    orig = cg.run_travel

    async def _slow_travel(token, code):
        inflight["n"] += 1
        inflight["max"] = max(inflight["max"], inflight["n"])
        await asyncio.sleep(0.01)
        inflight["n"] -= 1
        return cg.GrowthAction(kind="traveled", message="ok")

    monkeypatch.setattr(cg, "run_travel", _slow_travel)
    monkeypatch.setattr(cg, "accept_automatable_tasks",
                        lambda t, c: asyncio.sleep(0.01, result=cg.GrowthAction(
                            kind="accepted", message="ok")))
    targets = [cg.GrowthTarget("codebuddy_cn", f"u{i}", "tok") for i in range(4)]
    out = asyncio.run(cg.run_growth_batch(targets, do_travel=True, do_tasks=True, notify=False))
    assert inflight["max"] == 1, "必须严格串行"
    assert len(out) == 8  # 4 账号 × 2 动作
    monkeypatch.setattr(cg, "run_travel", orig)


def test_run_growth_batch_continues_after_failure(monkeypatch):
    """单账号失败不中断整批。"""
    async def _boom(token, code):
        raise RuntimeError("boom")

    monkeypatch.setattr(cg, "run_travel", _boom)
    monkeypatch.setattr(cg, "accept_automatable_tasks",
                        lambda t, c: asyncio.sleep(0, result=cg.GrowthAction(kind="already")))
    targets = [cg.GrowthTarget("codebuddy_cn", f"u{i}", "tok") for i in range(3)]
    out = asyncio.run(cg.run_growth_batch(targets, do_travel=True, do_tasks=True, notify=False))
    assert len(out) == 6
    assert sum(1 for r in out if r["kind"] == "failed") == 3
    assert sum(1 for r in out if r["kind"] == "already") == 3


def test_run_growth_batch_respects_action_flags(monkeypatch):
    """do_travel=False / do_tasks=False 时对应动作完全不执行。"""
    calls = []
    monkeypatch.setattr(cg, "run_travel",
                        lambda t, c: asyncio.sleep(0, result=cg.GrowthAction(kind="traveled")))
    monkeypatch.setattr(cg, "accept_automatable_tasks",
                        lambda t, c: asyncio.sleep(0, result=cg.GrowthAction(kind="accepted")))
    targets = [cg.GrowthTarget("codebuddy_cn", "u1", "tok")]
    out = asyncio.run(cg.run_growth_batch(targets, do_travel=False, do_tasks=True, notify=False))
    assert len(out) == 1 and out[0]["action"] == "task"
    out = asyncio.run(cg.run_growth_batch(targets, do_travel=True, do_tasks=False, notify=False))
    assert len(out) == 1 and out[0]["action"] == "travel"


# ── 按天幂等（DB 层）──────────────────────────────────────────

def _mk_db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from server.models.base import Base
    from server.models.growth_log import GrowthLog  # noqa: F401 触发注册
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False), Base


def test_already_done_today_idempotency():
    """当天已成功处理过 → True；failed 不算（必须允许重试）。"""
    from server.models.growth_log import GrowthLog

    async def _run():
        engine, Session, Base = _mk_db()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            assert await cg._already_done_today(db, "codebuddy_cn", "u1", "travel") is False
            db.add(GrowthLog(provider_code="codebuddy_cn", owner="u1", action="travel",
                             kind="failed", message="boom"))
            await db.commit()
            assert await cg._already_done_today(db, "codebuddy_cn", "u1", "travel") is False, \
                "failed 必须允许重试"
            db.add(GrowthLog(provider_code="codebuddy_cn", owner="u1", action="travel",
                             kind="traveled", message="ok"))
            await db.commit()
            assert await cg._already_done_today(db, "codebuddy_cn", "u1", "travel") is True
            # 不同动作互不影响
            assert await cg._already_done_today(db, "codebuddy_cn", "u1", "task") is False
            # 不同账号互不影响
            assert await cg._already_done_today(db, "codebuddy_cn", "u2", "travel") is False
        await engine.dispose()
    asyncio.run(_run())


def test_run_growth_batch_skips_when_done_today(monkeypatch):
    """当天已处理 → 跳过（不发上游请求）。"""
    from server.models.growth_log import GrowthLog

    async def _run():
        engine, Session, Base = _mk_db()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        called = []
        monkeypatch.setattr(cg, "run_travel",
                            lambda t, c: called.append("t") or asyncio.sleep(
                                0, result=cg.GrowthAction(kind="traveled")))
        async with Session() as db:
            db.add(GrowthLog(provider_code="codebuddy_cn", owner="u1", action="travel",
                             kind="traveled"))
            await db.commit()
            targets = [cg.GrowthTarget("codebuddy_cn", "u1", "tok")]
            out = await cg.run_growth_batch(targets, db=db, do_travel=True,
                                            do_tasks=False, notify=False)
            assert out[0]["kind"] == "already" and "跳过" in out[0]["message"]
            assert not called, "已完成的动作不得再打上游"
        await engine.dispose()
    asyncio.run(_run())


def test_growth_log_model_has_expected_columns():
    from server.models.growth_log import GrowthLog
    cols = {c.name for c in GrowthLog.__table__.columns}
    for want in ("id", "created_at", "trigger", "provider_code", "owner", "action",
                 "kind", "credit", "energy", "location", "task_codes", "message",
                 "error", "duration_ms"):
        assert want in cols, f"缺列 {want}"


# ── 配置与调度接线守卫 ───────────────────────────────────────

def test_growth_config_defaults():
    from server.config import GrowthConfig
    c = GrowthConfig()
    assert c.enabled is True and c.travel is True and c.tasks is True
    assert c.hour == 11 and c.minute == 0, "必须与签到（10:30）错开"
    assert c.startup_catchup is True


def test_growth_config_registered_on_config():
    from server.config import Config
    assert "growth" in Config.model_fields


def test_scheduler_wired():
    import inspect
    from server import main
    src = inspect.getsource(main)
    assert "_register_growth_job" in src
    assert 'id="growth_daily"' in src
    assert "timezone=\"Asia/Shanghai\"" in src
    assert "_growth_startup_catchup" in src
    assert "_growth_task = _aio.ensure_future(_growth_startup_catchup())" in src
    assert "reschedule_growth_job" in src


def test_endpoints_registered():
    """端点必须挂在 /admin/api 前缀下（否则未登录返回 HTML 而非 401 JSON）。"""
    from server.api.checkin_router import router
    paths = {r.path for r in router.routes}
    for want in ("/admin/api/checkin/growth", "/admin/api/checkin/growth/run",
                 "/admin/api/checkin/growth/config"):
        assert want in paths, f"缺端点 {want}"


def test_growth_log_registered_in_db():
    import inspect
    from server import db
    src = inspect.getsource(db)
    assert "from .models.growth_log import GrowthLog" in src
    assert "idx_growth_prov_owner_time" in src

# ── 盲盒 / 抽奖自动执行（2026-09-28，P2）─────────────────────
# 消耗性操作的三道克制约束必须锁死：单次 1 个、运行上限、client_token 防重放。

def test_run_gacha_opens_one_per_request_and_reports_wins(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [
        _ok({"affordable": 1, "balance": 13, "cost_per_open": 10, "max_open_count": 5}),
        _ok({"results": [{"instance": {"name": "咖啡猫", "rarity": "SR"},
                          "template": {"name": "咖啡猫", "rarity": "SR"}}]}),
    ])
    out = asyncio.run(cg.run_gacha("tok", "codebuddy_cn"))
    assert out.ok and out.kind == "claimed"
    assert "咖啡猫" in out.message and "SR" in out.message
    assert out.detail["opened"] == 1
    assert calls[0]["url"].endswith("/activity/growth/buddy/quota")
    assert calls[1]["url"].endswith("/v2/activity/growth/buddy/open")
    assert json.loads(calls[1]["content"]) == {"count": 1}, "每次只开 1 个"


def test_run_gacha_respects_run_cap(monkeypatch):
    """affordable=7 也只开 5 个（上游 max_open_count=5，防烧光库存）。"""
    results = [_ok({"affordable": 7, "balance": 70, "cost_per_open": 10})]
    results += [_ok({"results": [{"instance": {"name": f"猫{i}"}}]}) for i in range(6)]
    calls = []
    _patch(monkeypatch, calls, results)
    out = asyncio.run(cg.run_gacha("tok", "codebuddy_cn"))
    opens = [c for c in calls if c["url"].endswith("/buddy/open")]
    assert len(opens) == cg.GACHA_MAX_OPEN_PER_RUN == 5
    assert out.detail["opened"] == 5


def test_run_gacha_zero_affordable_is_noop(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [_ok({"affordable": 0, "balance": 3, "cost_per_open": 10})])
    out = asyncio.run(cg.run_gacha("tok", "codebuddy_cn"))
    assert out.kind == "already"
    assert all(not c["url"].endswith("/buddy/open") for c in calls), "无可开不得发写请求"


def test_run_gacha_first_failure_is_retryable(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [
        _ok({"affordable": 2, "balance": 20, "cost_per_open": 10}),
        (200, {"code": 500, "msg": "开盒过于频繁"}, None),
    ])
    out = asyncio.run(cg.run_gacha("tok", "codebuddy_cn"))
    assert out.kind == "failed", "一个都没开成必须报 failed（允许重试）"


def test_run_gacha_midway_failure_keeps_wins(monkeypatch):
    """已开到 1 个后中途失败：kind=claimed 保留战果，不丢也不虚报。"""
    calls = []
    _patch(monkeypatch, calls, [
        _ok({"affordable": 3, "balance": 30, "cost_per_open": 10}),
        _ok({"results": [{"instance": {"name": "猫A", "rarity": "R"}}]}),
        (200, {"code": 500, "msg": "开盒过于频繁"}, None),
    ])
    out = asyncio.run(cg.run_gacha("tok", "codebuddy_cn"))
    assert out.kind == "claimed"
    assert out.detail["opened"] == 1 and "猫A" in out.message


def test_lottery_draw_fresh_client_token_each_draw(monkeypatch):
    """client_token 是上游防重放设计：每次抽奖必须换新 UUID。"""
    calls = []
    _patch(monkeypatch, calls, [
        _ok({"balance": 3}),
        _ok({"prize_name": "100 积分"}),
        _ok({"credit_granted": 5}),
        _ok({"energy_granted": 2}),
    ])
    out = asyncio.run(cg.run_lottery("tok", "codebuddy_cn"))
    assert out.kind == "claimed"
    posts = [c for c in calls if c["url"].endswith("/lottery/draw")]
    assert len(posts) == 3
    tokens = [json.loads(c["content"])["client_token"] for c in posts]
    assert len(set(tokens)) == 3, "client_token 每次必须新生成"
    assert all(t.startswith("draw-") and len(t) > 10 for t in tokens)
    assert "100 积分" in out.message and "5 积分" in out.message and "2 能量" in out.message


def test_run_lottery_zero_chances_is_noop(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [_ok({"balance": 0})])
    out = asyncio.run(cg.run_lottery("tok", "codebuddy_cn"))
    assert out.kind == "already"
    assert all(not c["url"].endswith("/lottery/draw") for c in calls)


def test_run_lottery_run_cap(monkeypatch):
    results = [_ok({"balance": 99})]
    results += [_ok({"prize_name": f"奖{i}"}) for i in range(11)]
    calls = []
    _patch(monkeypatch, calls, results)
    out = asyncio.run(cg.run_lottery("tok", "codebuddy_cn"))
    draws = [c for c in calls if c["url"].endswith("/lottery/draw")]
    assert len(draws) == cg.LOTTERY_MAX_DRAW_PER_RUN == 10


def test_batch_gacha_lottery_default_off_and_flags_work(monkeypatch):
    """run_growth_batch 默认不碰盲盒/抽奖（消耗性）；显式传 flag 才执行。"""
    gacha_calls = []

    async def _g(token, code):
        gacha_calls.append(1)
        return cg.GrowthAction(kind="claimed", message="win")

    monkeypatch.setattr(cg, "run_gacha", _g)
    monkeypatch.setattr(cg, "run_lottery",
                        lambda t, c: asyncio.sleep(0, result=cg.GrowthAction(kind="claimed")))
    monkeypatch.setattr(cg, "run_travel",
                        lambda t, c: asyncio.sleep(0, result=cg.GrowthAction(kind="traveled")))
    monkeypatch.setattr(cg, "accept_automatable_tasks",
                        lambda t, c: asyncio.sleep(0, result=cg.GrowthAction(kind="accepted")))
    targets = [cg.GrowthTarget("codebuddy_cn", "u1", "tok")]
    out = asyncio.run(cg.run_growth_batch(targets, do_travel=True, do_tasks=True, notify=False))
    assert not gacha_calls and len(out) == 2, "默认必须不碰盲盒/抽奖"
    out = asyncio.run(cg.run_growth_batch(targets, do_travel=False, do_tasks=False,
                                          do_gacha=True, do_lottery=True, notify=False))
    assert len(out) == 2 and {r["action"] for r in out} == {"gacha", "lottery"}
    assert gacha_calls


def test_manual_trigger_bypasses_daily_done_for_consumptive_actions(monkeypatch):
    """手动点按钮 = 明确授权：当天定时跑过（含 no-op）后手动仍可再执行盲盒/抽奖；
    定时触发仍受按天幂等约束（防重复烧库存）。"""
    from server.models.growth_log import GrowthLog

    async def _g(token, code):
        return cg.GrowthAction(kind="claimed", message="win")

    monkeypatch.setattr(cg, "run_gacha", _g)

    async def _run():
        engine, Session, Base = _mk_db()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            db.add(GrowthLog(provider_code="codebuddy_cn", owner="u1", action="gacha",
                             kind="already"))
            await db.commit()
            targets = [cg.GrowthTarget("codebuddy_cn", "u1", "tok")]
            out = await cg.run_growth_batch(targets, trigger="scheduled", db=db,
                                            do_travel=False, do_tasks=False,
                                            do_gacha=True, notify=False)
            assert out[0]["kind"] == "already" and "跳过" in out[0]["message"]
            out = await cg.run_growth_batch(targets, trigger="manual", db=db,
                                            do_travel=False, do_tasks=False,
                                            do_gacha=True, notify=False)
            assert out[0]["kind"] == "claimed"
        await engine.dispose()
    asyncio.run(_run())


def test_growth_config_gacha_lottery_default_off():
    from server.config import GrowthConfig
    c = GrowthConfig()
    assert c.gacha is False and c.lottery is False, "消耗性操作默认必须关"
