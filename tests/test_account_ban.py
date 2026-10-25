# -*- coding: utf-8 -*-
"""OAuth 账号封禁自动识别（2026-10-25）。

需求原话：「codebuddy国际版被封了，帮我做成自动识别的，封了后就直接在 OAuth 那
显示哪个被封了」。

生产取证（codebuddy_intl 两账号 60 次失败，2026-10-24）给出了封号的真实报文：
    HTTP 403 {"code":11140,"msg":"request illegal",
              "displayMsg":{"en":"Your account has been restricted due to unusual
              activity","zh":"账号存在异常行为，已被限制使用"}}
与既有的 11128 风控拦截**共用 msg=request illegal**，区别只在 displayMsg：
    11128 → "Request blocked. Please send it again"（请求形态问题，补头/换号可解）
    11140 → "Your account has been restricted…"（账号终态，只有人工申诉能解）

改前行为：11140 被 is_risk_block_text 认成风控 → 冷却 30 分钟 → 1h 后重试再失败，
两个账号的生产 fail_count 分别累积到 21 / 41。本文件锁住修复后的四条语义。
"""
import inspect
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.oauth_token import OAuthToken


# 生产原文（截到 displayMsg 结束；收集到的 60 条样本形态一致）
BANNED_403 = (
    "upstream_call_failed: HTTPStatusError: Client error '403 Forbidden' for url "
    "'https://www.codebuddy.ai/v2/chat/completions'\nResponse: "
    '{"code":11140,"msg":"request illegal","requestId":"9817b2c3b3104f41a4c3662de27dc890",'
    '"displayMsg":{"en":"Your account has been restricted due to unusual activity",'
    '"zh":"账号存在异常行为，已被限制使用","zh-hant":"帳號存在異常行為，已被限制使用"}}'
)

# 11128 风控（同样 msg=request illegal，但 displayMsg 完全不同）
RISK_400 = (
    "upstream_call_failed: HTTPStatusError: Client error '400 Bad Request' for url "
    "'https://www.codebuddy.ai/v2/chat/completions'\nResponse: "
    '{"code":11128,"msg":"request illegal","requestId":"53c5b253-9ea4-4b27-8a64-7523bc840492",'
    '"displayMsg":{"en":"Request blocked. Please send it again",'
    '"zh":"请求被拦截，请重新发送","zh-hant":"請求已被攔截，請重新發送"}}'
)


class _FakeCrypto:
    def encrypt(self, s): return "enc-" + s
    def decrypt(self, s): return (s[4:] if s.startswith("enc-") else s)


async def _setup(tmp_path, owners=("acct-a", "acct-b")):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(OAuthToken.__table__.create)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        db.add_all([
            OAuthToken(provider_code="codebuddy_intl", owner=o,
                       access_token_enc=f"enc-tok-{o[-1]}", is_active=True)
            for o in owners
        ])
        await db.commit()
    return engine, Session


# ── 1) 判定：封号 vs 风控必须分开 ─────────────────────────────

def test_banned_marker_recognized_from_production_payload():
    from server.core.error_text import is_account_banned_text
    assert is_account_banned_text(BANNED_403)
    # 报文被截断到只剩 displayMsg 前半段时仍要命中（生产 error_msg 有 400 字上限）
    assert is_account_banned_text('{"code":11140,"displayMsg":{"en":"Your account has been restricted')
    # 中文变体单独出现也要命中
    assert is_account_banned_text("账号存在异常行为，已被限制使用")


def test_risk_block_is_not_treated_as_ban():
    """11128「请求被拦截」是请求形态问题，不能当成封号永久拉黑。"""
    from server.core.error_text import is_account_banned_text, is_risk_block_text
    assert not is_account_banned_text(RISK_400)
    assert is_risk_block_text(RISK_400)          # 仍是风控（走 30 分钟冷却档）
    # 封号也属于「账号打不通」→ 仍要过 is_account_worthy_error 资格闸
    assert is_risk_block_text(BANNED_403)
    from server.core.credential_resolver import is_account_worthy_error
    assert is_account_worthy_error(BANNED_403, 403)
    # 空/无关文本不误判
    assert not is_account_banned_text("")
    assert not is_account_banned_text(None)
    assert not is_account_banned_text("empty_output: upstream 返回 200 但无内容")


def test_banned_reason_prefers_upstream_chinese_display():
    """落库原因优先上游中文原话（连接页直接展示给用户）。"""
    from server.core.oauth_client import _banned_reason_text
    assert _banned_reason_text(BANNED_403) == "账号存在异常行为，已被限制使用"
    assert len(_banned_reason_text(BANNED_403)) <= 500
    # 没有 displayMsg → 退回原文截断，不能抛
    assert _banned_reason_text("HTTP 403 forbidden") == "HTTP 403 forbidden"
    assert _banned_reason_text("")


# ── 2) 落库：封号写持久状态，不进冷却 ────────────────────────

@pytest.mark.asyncio
async def test_note_request_result_records_ban_not_cooldown(tmp_path, monkeypatch):
    """封号 → banned_at/banned_reason；cooldown_until 必须为空（否则界面两种状态并存）。"""
    from server.core.oauth_client import get_oauth_client
    import server.core.oauth_client as oc
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    try:
        await client.note_request_result("codebuddy_intl", "acct-a", ok=False,
                                         status_code=403, error=BANNED_403,
                                         model_id="deepseek-v4.1-flash")
        async with Session() as db:
            row = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
        assert row.banned_at is not None
        assert row.banned_reason == "账号存在异常行为，已被限制使用"
        # 封禁不走冷却分支，也不写 (账号×模型) 限额记录
        assert row.cooldown_until is None
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_intl", "acct-a"))
        await engine.dispose()


@pytest.mark.asyncio
async def test_ban_does_not_refresh_timestamp_on_repeat(tmp_path, monkeypatch):
    """重复报封禁时保留首次判定时间（用户据此判断封了多久）。"""
    from server.core.oauth_client import get_oauth_client
    import server.core.oauth_client as oc
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    first = datetime.utcnow() - timedelta(days=3)
    try:
        async with Session() as db:
            row = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
            row.banned_at = first
            row.banned_reason = "old"
            await db.commit()
        await client.note_request_result("codebuddy_intl", "acct-a", ok=False,
                                         status_code=403, error=BANNED_403)
        async with Session() as db:
            row = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
        assert row.banned_at == first            # 时间点不刷新
        assert row.banned_reason == "账号存在异常行为，已被限制使用"   # 原因刷新为最新
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_intl", "acct-a"))
        await engine.dispose()


# ── 3) 路由：封禁账号不被选中 ────────────────────────────────

@pytest.mark.asyncio
async def test_pick_skips_banned_account(tmp_path, monkeypatch):
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        # acct-a 被排在最前（id 小、__default 之外先比 id），先确认基准
        async with Session() as db:
            assert (await client.pick_access_token_ex("codebuddy_intl", db))[0] == "acct-a"
            row = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
            row.banned_at = datetime.utcnow()
            row.banned_reason = "账号存在异常行为，已被限制使用"
            await db.commit()
            # 封禁后自动选号必须落到 acct-b
            assert (await client.pick_access_token_ex("codebuddy_intl", db))[0] == "acct-b"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pick_all_banned_returns_none(tmp_path, monkeypatch):
    """全部账号封禁 → 返回 None（调用方给出封禁报错），不拿必定失败的号去撞。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            rows = (await db.execute(select(OAuthToken))).scalars().all()
            for r in rows:
                r.banned_at = datetime.utcnow()
            await db.commit()
            assert await client.pick_access_token_ex("codebuddy_intl", db) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pick_explicit_owner_banned_is_rejected_locally(tmp_path, monkeypatch):
    """点名账号已封禁 → 本地直接拒绝（不白烧一次上游往返）。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            row = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
            row.banned_at = datetime.utcnow()
            await db.commit()
            assert await client.pick_access_token_ex(
                "codebuddy_intl", db, owner="acct-a") is None
            # 未封禁的点名账号照常可用
            assert (await client.pick_access_token_ex(
                "codebuddy_intl", db, owner="acct-b"))[0] == "acct-b"
    finally:
        await engine.dispose()


# ── 4) 解除：只认人工重新登录，不被自动刷新擦掉 ───────────────

def test_ban_clear_only_on_relogin_source_guard():
    """_save_token 里的解除必须以 update_existing is None 为条件。

    自动刷新全部走 update_existing=existing；若无条件清标记，每几十分钟一次的
    例行刷新就会把封标擦掉，账号又被选号打上游 → 封禁识别形同失效。
    """
    from server.core.oauth_client import OAuthClient
    src = inspect.getsource(OAuthClient._save_token)
    assert 'update_existing is None' in src
    assert "banned_at = None" in src


@pytest.mark.asyncio
async def test_relogin_clears_ban(tmp_path, monkeypatch):
    from server.core.oauth_client import get_oauth_client
    import server.core.oauth_client as oc
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    try:
        async with Session() as db:
            row = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
            row.banned_at = datetime.utcnow()
            row.banned_reason = "账号存在异常行为，已被限制使用"
            await db.commit()
            # 人工重新登录（update_existing 为空）
            await client._save_token(db, "codebuddy_intl", "acct-a", {
                "access_token": "tok-a", "refresh_token": "ref-a", "expires_in": 3600})
            row = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
            assert row.banned_at is None and row.banned_reason == ""
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_intl", "acct-a"))
        await engine.dispose()


# ── 5) 展示：连接列表带上封禁字段 ────────────────────────────

@pytest.mark.asyncio
async def test_list_connections_exposes_ban_fields(tmp_path, monkeypatch):
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    when = datetime(2026, 10, 24, 15, 16, 25)
    try:
        async with Session() as db:
            row = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
            row.banned_at = when
            row.banned_reason = "账号存在异常行为，已被限制使用"
            await db.commit()
            conns = {c["owner"]: c for c in await client.list_connections(db)}
        assert conns["acct-a"]["banned_at"] == "2026-10-24T15:16:25Z"
        assert conns["acct-a"]["banned_reason"] == "账号存在异常行为，已被限制使用"
        assert conns["acct-b"]["banned_at"] is None
        assert conns["acct-b"]["banned_reason"] == ""
    finally:
        await engine.dispose()


def test_frontend_shows_banned_badge_and_reason():
    """OAuth 连接页要有「已封禁」徽标、原因行与恢复按钮。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "client/src/views/OAuthConnections.vue").read_text(
        encoding="utf-8")
    assert "conn-banned" in src
    assert "已封禁" in src
    assert "banned_reason" in src
    assert "setBanned" in src
    # 徽标判定必须优先于冷却/过期（封禁不会自动恢复）
    idx_ban = src.index("if (c.banned_at) return { label: '已封禁'")
    idx_inactive = src.index("if (!c.is_active)")
    assert idx_ban < idx_inactive


def test_usage_endpoint_marks_ban():
    """额度接口 403+11140 → 返回 banned 标记（用户点「查询余额」即可发现封禁）。"""
    from server.core import oauth_usage
    src = inspect.getsource(oauth_usage._codebuddy_usage)
    assert "is_account_banned_text" in src
    assert '"banned": True' in src
    # 路由层要把结果回写账号状态
    from server.api import oauth_router
    src2 = inspect.getsource(oauth_router.connection_usage)
    assert "_record_account_banned" in src2


def test_migration_declares_ban_columns():
    """老库升级必须补列（幂等迁移列表里要有）。"""
    from server import db as dbmod
    src = inspect.getsource(dbmod.init_db)
    assert "oauth_tokens ADD COLUMN banned_at" in src
    assert "oauth_tokens ADD COLUMN banned_reason" in src


# ── 6) 人工兜底：PATCH 手动置位/解除 ──────────────────────

@pytest.mark.asyncio
async def test_manual_ban_and_restore_via_patch(tmp_path, monkeypatch):
    """申诉成功后可手动恢复；恢复时必须连带清掉冷却/失败计数。

    否则「恢复」后账号仍带着旧冷却，界面会显示矛盾状态、选号也会继续跳过它。
    """
    from server.api.oauth_router import update_connection, ConnectionUpdate
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            row = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
            cid = row.id
            # 手动置位
            r = await update_connection(cid, ConnectionUpdate(banned=True), db)
            assert r["banned_at"] is not None
            assert r["banned_reason"] == "手动标记为已封禁"
            row = await db.get(OAuthToken, cid)
            assert row.banned_at is not None
            # 恢复：同时清掉冷却与失败计数
            row.cooldown_until = datetime.utcnow() + timedelta(hours=1)
            row.fail_count = 7
            await db.commit()
            r2 = await update_connection(cid, ConnectionUpdate(banned=False), db)
            assert r2["banned_at"] is None
            row = await db.get(OAuthToken, cid)
            assert row.banned_at is None and row.banned_reason == ""
            assert row.cooldown_until is None and row.fail_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_scheduler_skips_banned_guard():
    """刷新调度器必须跳过封禁账号（刷新只会白打上游端点）。"""
    from server.api import oauth_router
    src = inspect.getsource(oauth_router._refresh_scheduler_loop)
    assert "banned_at" in src
