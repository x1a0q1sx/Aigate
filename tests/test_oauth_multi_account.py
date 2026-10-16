# -*- coding: utf-8 -*-
"""OAuth 多账号治理 + free_tier 冷却 + 日志账号列（2026-10-13，F40）。

覆盖用户四项需求：
  3) 请求日志详情显示本次用的账号（routed_account 列 + 全路径写入 + 前端）
  4) 多账号自动选号：按「积分最近到期」优先（先用快过期的）
  5) 积分包耗尽后不再参与「最早到期」统计（展示层口径）
  6) 多账号报错 → 对应账号冷却（自动选号跳过；点名不受影响）
另有任务 1 的网关侧修复：free_tier 失败补模型冷却（is_free_failure_coolable）。
"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.oauth_token import OAuthToken
from server.models.base import Base


# ── 基础设施（仿 test_provider_scheduled_refresh） ──────────────

class _FakeCrypto:
    def encrypt(self, s): return "enc-" + s
    def decrypt(self, s): return (s[4:] if s.startswith("enc-") else s)


async def _db(tmp_path, tables):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    async with engine.begin() as conn:
        for t in tables:
            await conn.run_sync(t.create)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# ── 4/5) 额度排序键（按到期选号 + 耗尽包不参与最早到期） ──

def _q(remaining, expires=None, total=100, used=None):
    q = {"used": used if used is not None else (total - remaining),
         "total": total, "remaining": remaining, "unlimited": False}
    if expires:
        q["reset_at"] = expires
    return q


def test_quota_rank_prefers_earliest_expiry_with_balance():
    from server.core.oauth_usage import quota_rank_key
    now = datetime.utcnow()
    soon = (now + timedelta(days=1)).isoformat()
    late = (now + timedelta(days=30)).isoformat()
    rich = {"quotas": {"Bonus Pack 1": _q(100, soon), "Bonus Pack 2": _q(500, late)}}
    late_rich = {"quotas": {"plan": _q(9999, late)}}
    # 有余额且最早到期在前 → 排序键更小（优先）
    assert quota_rank_key(rich) < quota_rank_key(late_rich)


def test_quota_rank_exhausted_packs_do_not_affect_earliest():
    """已耗尽的包不参与「最早到期」——两个都还有余额的包谁更早谁优先。"""
    from server.core.oauth_usage import quota_rank_key
    now = datetime.utcnow()
    dead_soon = (now + timedelta(hours=1)).isoformat()   # 耗尽但到期最近
    a_soon = (now + timedelta(days=2)).isoformat()
    b_late = (now + timedelta(days=20)).isoformat()
    acct_a = {"quotas": {"dead": _q(0, dead_soon), "alive": _q(10, a_soon)}}
    acct_b = {"quotas": {"alive": _q(10, b_late)}}
    assert quota_rank_key(acct_a) < quota_rank_key(acct_b)


def test_quota_rank_ordering_balance_unknown_exhausted():
    from server.core.oauth_usage import quota_rank_key
    now = datetime.utcnow()
    has = {"quotas": {"p": _q(5, (now + timedelta(days=1)).isoformat())}}
    unknown = {"quotas": {}, "message": "接口失败"}
    exhausted = {"quotas": {"p": _q(0, (now + timedelta(days=9)).isoformat())}}
    assert quota_rank_key(has)[0] == 0
    assert quota_rank_key(unknown)[0] == 1
    assert quota_rank_key(exhausted)[0] == 2
    assert quota_rank_key(has) < quota_rank_key(unknown) < quota_rank_key(exhausted)


def test_quota_rank_unlimited_counts_as_balance():
    from server.core.oauth_usage import quota_rank_key
    unlimited = {"quotas": {"p": {"unlimited": True}}}
    exhausted = {"quotas": {"p": _q(0)}}
    assert quota_rank_key(unlimited) < quota_rank_key(exhausted)


# ── 6) 账号级冷却 + 自动选号 ───────────────────────────────────

async def _oauth_setup(tmp_path):
    engine, Session = await _db(tmp_path, [OAuthToken.__table__])
    async with Session() as db:
        db.add_all([
            OAuthToken(provider_code="codebuddy_intl", owner="acct-a",
                       access_token_enc="enc-tok-a", is_active=True),
            OAuthToken(provider_code="codebuddy_intl", owner="acct-b",
                       access_token_enc="enc-tok-b", is_active=True),
        ])
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_pick_skips_cooling_account(tmp_path, monkeypatch):
    """acct-a 冷却中 → 自动选号落到 acct-b（并返回实际 owner）。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _oauth_setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            row = (await db.execute(
                __import__("sqlalchemy").select(OAuthToken).where(OAuthToken.owner == "acct-a")
            )).scalar_one()
            row.cooldown_until = datetime.utcnow() + timedelta(minutes=5)
            row.fail_count = 2
            await db.commit()
            got = await client.pick_access_token_ex("codebuddy_intl", db)
            assert got == ("acct-b", "tok-b")
            # 冷却期外的过期冷却不拦人
            row.cooldown_until = datetime.utcnow() - timedelta(seconds=1)
            await db.commit()
            got2 = await client.pick_access_token_ex("codebuddy_intl", db)
            assert got2[0] == "acct-a"   # id 最早者在可用时优先（无额度缓存→旧语义）
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pick_all_cooling_still_falls_back(tmp_path, monkeypatch):
    """全部冷却 → 仍回退使用（不能让路由直接 503）。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _oauth_setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            for owner in ("acct-a", "acct-b"):
                row = (await db.execute(
                    __import__("sqlalchemy").select(OAuthToken).where(OAuthToken.owner == owner)
                )).scalar_one()
                row.cooldown_until = datetime.utcnow() + timedelta(minutes=9)
                await db.commit()
            got = await client.pick_access_token_ex("codebuddy_intl", db)
            assert got is not None
            assert got[0] in ("acct-a", "acct-b")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pick_explicit_owner_ignores_cooldown(tmp_path, monkeypatch):
    """点名要的账号不受冷却影响（用户明确指定就照用）。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _oauth_setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            row = (await db.execute(
                __import__("sqlalchemy").select(OAuthToken).where(OAuthToken.owner == "acct-a")
            )).scalar_one()
            row.cooldown_until = datetime.utcnow() + timedelta(minutes=5)
            await db.commit()
            got = await client.pick_access_token_ex("codebuddy_intl", db, owner="acct-a")
            assert got == ("acct-a", "tok-a")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_usage_rank_selects_earliest_expiry_account(tmp_path, monkeypatch):
    """有额度缓存时：积分更早到期（且有余额）的账号优先于 id 最早的。"""
    import server.core.oauth_usage as ou
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _oauth_setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    # acct-a（id 最早）月底到期；acct-b 明天到期 → b 应该被选中
    now = datetime.utcnow()
    monkeypatch.setattr(ou, "_CACHE", {
        ou._key("codebuddy_intl", "tok-a"): (ou.time.monotonic() + 999,
            {"quotas": {"p": _q(100, (now + timedelta(days=30)).isoformat())}}),
        ou._key("codebuddy_intl", "tok-b"): (ou.time.monotonic() + 999,
            {"quotas": {"p": _q(50, (now + timedelta(days=1)).isoformat())}}),
    })
    try:
        async with Session() as db:
            got = await client.pick_access_token_ex("codebuddy_intl", db)
            assert got == ("acct-b", "tok-b")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_request_result_cooldown_and_recovery(tmp_path, monkeypatch):
    """失败 → fail_count+1 + cooldown_until；成功 → 清零（脏标记路径）。"""
    from server.core.oauth_client import get_oauth_client
    import server.core.oauth_client as oc
    engine, Session = await _oauth_setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    # note_request_result 内部自建 Session，需指向测试库
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    try:
        await client.note_request_result("codebuddy_intl", "acct-a", ok=False, status_code=429)
        async with Session() as db:
            row = (await db.execute(
                __import__("sqlalchemy").select(OAuthToken).where(OAuthToken.owner == "acct-a")
            )).scalar_one()
            assert row.fail_count == 1
            assert row.cooldown_until is not None and row.cooldown_until > datetime.utcnow()
            cd1 = row.cooldown_until
        # 429 第二次失败进入更长冷却（指数退避 60→120）
        await client.note_request_result("codebuddy_intl", "acct-a", ok=False, status_code=429)
        async with Session() as db:
            row = (await db.execute(
                __import__("sqlalchemy").select(OAuthToken).where(OAuthToken.owner == "acct-a")
            )).scalar_one()
            assert row.fail_count == 2
            assert row.cooldown_until > cd1
        # 成功 → 清零（注意脏标记在 note_request_result 内维护）
        await client.note_request_result("codebuddy_intl", "acct-a", ok=True)
        async with Session() as db:
            row = (await db.execute(
                __import__("sqlalchemy").select(OAuthToken).where(OAuthToken.owner == "acct-a")
            )).scalar_one()
            assert row.fail_count == 0 and row.cooldown_until is None
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_intl", "acct-a"))
        await engine.dispose()


def test_account_worthy_error_classification():
    from server.core.credential_resolver import is_account_worthy_error
    assert is_account_worthy_error("HTTP 429: Rate limit exceeded")
    assert is_account_worthy_error("FreeUsageLimitError: Rate limit exceeded")
    assert is_account_worthy_error("usage exceeds frequency limit")
    assert is_account_worthy_error("x", status_code=401)
    assert is_account_worthy_error("x", status_code=503)
    # 上游账号级风控拦截（CodeBuddy 国际版 11128）：HTTP 400 也是「这个账号打不通」
    # —— 2026-10-16 实测：同 payload 换账号 12/12 通过、被拦账号 9/9 全败
    assert is_account_worthy_error(
        "upstream_call_failed: HTTPStatusError: Client error '400 Bad Request' for url "
        "'https://www.codebuddy.ai/v2/chat/completions'\nResponse: {\"code\":11128,"
        "\"msg\":\"request illegal\",\"displayMsg\":{\"zh\":\"请求被拦截，请重新发送\"}}")
    assert is_account_worthy_error("这次调用没通过渠道校验，可能不是官方客户端发出的。请用官方版本重新发送。")
    # 内容类问题不该冷却账号
    assert not is_account_worthy_error("empty_output: upstream 返回 200 但无内容")
    assert not is_account_worthy_error("race_overtaken: 15s 内无返回")
    # 普通 400（请求体不合法）与风控无关，仍不冷却
    assert not is_account_worthy_error("HTTP 400: invalid request body", status_code=400)


def test_account_cooldown_seconds_risk_block_tier():
    """风控拦截不是瞬时故障 → 30 分钟基础档（否则 120s 一过又要牺牲请求去试探）。"""
    from server.core.oauth_client import OAuthClient
    assert OAuthClient._account_cooldown_seconds(400, 1) == 120
    assert OAuthClient._account_cooldown_seconds(400, 1, True) == 1800
    assert OAuthClient._account_cooldown_seconds(400, 2, True) == 3600   # 指数退避仍生效
    assert OAuthClient._account_cooldown_seconds(429, 1, True) == 1800   # 风控档优先于 429 档


@pytest.mark.asyncio
async def test_note_request_result_risk_block_sets_long_cooldown(tmp_path, monkeypatch):
    """11128 走到 note_request_result 时要真的进冷却，且直接用 30 分钟档。"""
    from server.core.oauth_client import get_oauth_client
    import server.core.oauth_client as oc
    engine, Session = await _oauth_setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    err = ("upstream_call_failed: HTTPStatusError: Client error '400 Bad Request' for url "
           "'https://www.codebuddy.ai/v2/chat/completions'\nResponse: "
           "{\"code\":11128,\"msg\":\"request illegal\"}")
    try:
        await client.note_request_result("codebuddy_intl", "acct-a", ok=False,
                                         status_code=400, error=err)
        async with Session() as db:
            row = (await db.execute(
                __import__("sqlalchemy").select(OAuthToken).where(OAuthToken.owner == "acct-a")
            )).scalar_one()
        assert row.fail_count == 1 and row.cooldown_until is not None
        delta = (row.cooldown_until - datetime.utcnow()).total_seconds()
        assert 1700 < delta <= 1800, f"风控档应为 1800s，实际 {delta}"
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_intl", "acct-a"))
        await engine.dispose()


def test_status_from_error_text_extraction():
    from server.core.oauth_client import status_from_error_text
    assert status_from_error_text('Client error \'429 Too Many Requests\'') == 429
    assert status_from_error_text("HTTP 401 unauthorized") == 401
    assert status_from_error_text("no code here") is None
    assert status_from_error_text(None) is None


# ── 任务 1：free_tier 失败冷却判据 ─────────────────────────────

def test_free_failure_coolable_markers():
    from server.core.free_providers import is_free_failure_coolable
    # 限流 / 端点不可用 / 下架 / 传输失败 → 冷却
    assert is_free_failure_coolable('Provider request failed with HTTP 429: {"type":"error","error":{"type":"FreeUsageLimitError","message":"Rate limit exceeded."}}')
    assert is_free_failure_coolable('Provider request failed with HTTP 429: {"error":{"type":"server_error","message":"Upstream request failed: Endpoint is unavailable."}}')
    assert is_free_failure_coolable('Provider request failed with HTTP 410: {"type":"error","error":{"type":"ModelDeprecated","message":"Model exo-free has been deprecated."}}')
    assert is_free_failure_coolable('{"type": "unknown", "message": "HTTP transport failed"}')
    # 会话/权限配置类确定性错误 → 不冷却（改配置即恢复）
    assert not is_free_failure_coolable('FreeTierError: OpenCode free tier can only be used from within OpenCode.')
    assert not is_free_failure_coolable("")


# ── 3) 日志账号列：DB 模型 + 迁移 + 全路径静态锁 ───────────────

def test_request_log_has_routed_account_column():
    from server.models.request_log import RequestLog
    assert "routed_account" in RequestLog.__table__.columns


def test_oauth_token_has_cooldown_columns():
    assert "cooldown_until" in OAuthToken.__table__.columns
    assert "fail_count" in OAuthToken.__table__.columns


def test_migrations_include_new_columns():
    src = Path("server/db.py").read_text(encoding="utf-8")
    assert "ALTER TABLE oauth_tokens ADD COLUMN cooldown_until" in src
    assert "ALTER TABLE oauth_tokens ADD COLUMN fail_count" in src
    assert "ALTER TABLE request_logs ADD COLUMN routed_account" in src


def test_credential_resolver_carries_account():
    src = Path("server/core/credential_resolver.py").read_text(encoding="utf-8")
    assert "rc.account, token = got[0], got[1]" in src
    assert "pick_access_token_ex" in src


def test_v1_router_writes_routed_account_everywhere():
    """直连（非流式中央日志 + 流式）、auto 级联（非流式失败/成功、流式失败/成功）、
    combo（流式成功/失败、非流式成功）都要带 routed_account。"""
    src = Path("server/api/v1_router.py").read_text(encoding="utf-8")
    assert "routed_account=getattr(route_result" in src or "routed_account=getattr(route_result, \"account\", None)" in src
    assert src.count("routed_account=") >= 6
    assert "note_account_result" in src


def test_admin_router_playground_writes_account():
    src = Path("server/api/admin_router.py").read_text(encoding="utf-8")
    assert "routed_account=getattr(_route_result" in src
    assert "_note_pg_account" in src


def test_logs_api_returns_routed_account():
    src = Path("server/api/admin_routing.py").read_text(encoding="utf-8")
    assert src.count('"routed_account"') >= 2


# ── 前端静态回归 ───────────────────────────────────────────────

def _client(rel):
    return Path("client/src", rel).read_text(encoding="utf-8")


def test_quota_panel_excludes_exhausted_from_earliest():
    src = _client("components/QuotaPanel.vue")
    assert "rem > 0 && q.reset_at" in src


def test_logs_panel_shows_account():
    src = _client("components/LogsPanel.vue")
    assert "routed_account" in src
    assert "使用账号" in src


def test_oauth_connections_shows_cooldown():
    src = _client("views/OAuthConnections.vue")
    assert "cooldown_until" in src
    assert "冷却中" in src


def test_oauth_usage_prewarm_wired():
    src = Path("server/api/oauth_router.py").read_text(encoding="utf-8")
    assert "_prewarm_multi_account_usage_once" in src
    main = Path("server/main.py").read_text(encoding="utf-8")
    assert "start_oauth_usage_prewarm" in main
