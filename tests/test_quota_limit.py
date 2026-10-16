# -*- coding: utf-8 -*-
"""每日限额（(账号×模型) 粒度）识别 / 解析 / 冷却记录（2026-10-16）。

生产报文（workbuddy / CodeBuddy 国服与国际服）：
- CN: {"code":6004,"msg":"您的使用量已超出频率限制，将在 <时间> UTC+8 重置，
       您也可以切换其他模型继续使用。"}
- Intl: {"code":6004,"msg":"usage exceeds frequency limit, ... your usage will reset at
         <时间> UTC+8, alternatively, you can switch to the other models ..."}

关键语义（本文件锁死）：
1) 识别：code 6004 / 中英文案命中；14003 too many requests（无重置时间）**不**命中；
2) 解析：UTC+8 后缀正确换算；已过期返回 None；无时间戳返回 None；
3) 记账：note_request_result 带 model_id + 限额文案 → 写 quota_limits 而非账号冷却；
4) 选号：该模型被限额的账号跳过，其他账号/其他模型不受影响；
5) 成功：同模型成功清限额记录；reset_at 过期后查询自动失效。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.oauth_token import OAuthToken
from server.models.quota_limit import QuotaLimit


def _utc8_future_msg(hours_ahead: int = 8, en: bool = False) -> str:
    """生成带「未来重置时间」的限额报文（UTC+8 格式），避免测试随真实时间过期。

    报文里的时间戳是 UTC+8 本地时间；期望的 reset_at（naive UTC）=
    utcnow() + hours_ahead。故时间戳 = utcnow() + hours_ahead + 8h。"""
    stamp_dt = (datetime.utcnow() + timedelta(hours=hours_ahead + 8)).replace(microsecond=0)
    stamp = stamp_dt.strftime("%Y-%m-%d %H:%M:%S")
    if en:
        return ('HTTPStatusError: Client error \'429 Too Many Requests\' for url '
                '\'https://www.codebuddy.ai/v2/chat/completions\'\nResponse: '
                '{"code":6004,"msg":"usage exceeds frequency limit, but don\'t worry, your '
                f'usage will reset at {stamp} UTC+8, alternatively, you can switch '
                'to the other models to continue"}')
    return ('upstream_call_failed: HTTPStatusError: Client error \'429 Too Many Requests\' '
            'for url \'https://copilot.tencent.com/v2/chat/completions\'\nResponse: '
            f'{{"code":6004,"msg":"您的使用量已超出频率限制，将在 {stamp} UTC+8 重置，'
            '您也可以切换其他模型继续使用。","requestId":"c9aa17a9"}')


def _expected_reset(hours_ahead: int = 8) -> datetime:
    return (datetime.utcnow() + timedelta(hours=hours_ahead)).replace(microsecond=0)


# 固定样例（识别用，识别本身不关心时间）
_CN_SAMPLE = ('upstream_call_failed: HTTPStatusError: Client error \'429 Too Many Requests\' '
              'for url \'https://copilot.tencent.com/v2/chat/completions\'\nResponse: '
              '{"code":6004,"msg":"您的使用量已超出频率限制，将在 2099-10-17 15:26:42 UTC+8 重置，'
              '您也可以切换其他模型继续使用。","requestId":"c9aa17a9"}')
_INTL_SAMPLE = ('HTTPStatusError: Client error \'429 Too Many Requests\' for url '
                '\'https://www.codebuddy.ai/v2/chat/completions\'\nResponse: '
                '{"code":6004,"msg":"usage exceeds frequency limit, but don\'t worry, your '
                'usage will reset at 2099-10-13 09:26:43 UTC+8, alternatively, you can switch '
                'to the other models to continue"}')


class _FakeCrypto:
    def encrypt(self, s): return "enc-" + s
    def decrypt(self, s): return (s[4:] if s.startswith("enc-") else s)


# ── 1) 识别 ─────────────────────────────────────────────────────

def test_is_daily_limit_text_cn_and_intl():
    from server.core.quota_limit import is_daily_limit_text
    assert is_daily_limit_text(_CN_SAMPLE)
    assert is_daily_limit_text(_INTL_SAMPLE)
    # 14003 瞬时频率限流（无重置时间/无 6004）不是每日限额
    assert not is_daily_limit_text(
        'HTTPStatusError: Client error \'429 Too Many Requests\'\nResponse: '
        '{"code":14003,"msg":"too many requests","retry_policy":{"retry":true}}')
    assert not is_daily_limit_text("empty_output: upstream 返回 200 但无内容")
    assert not is_daily_limit_text(None)
    assert not is_daily_limit_text("")


def test_is_quota_related_error_includes_gateway_marker():
    from server.core.quota_limit import is_quota_related_error
    assert is_quota_related_error(_CN_SAMPLE)
    assert is_quota_related_error("quota_limited: model 'step-5-preview' daily quota exhausted")
    assert not is_quota_related_error("HTTP 500 internal server error")


# ── 2) 重置时间解析 ──────────────────────────────────────────────

def test_parse_reset_utc8_cn():
    from server.core.quota_limit import parse_daily_limit_reset
    got = parse_daily_limit_reset(_CN_SAMPLE)
    assert got is not None
    # 2099-10-17 15:26:42 UTC+8 → 07:26:42 UTC
    assert got == datetime(2099, 10, 17, 7, 26, 42)


def test_parse_reset_utc8_intl():
    from server.core.quota_limit import parse_daily_limit_reset
    got = parse_daily_limit_reset(_INTL_SAMPLE)
    assert got == datetime(2099, 10, 13, 1, 26, 43)


def test_parse_reset_past_returns_none():
    """已过期时间（如补发旧日志）→ None，不建记录。"""
    from server.core.quota_limit import parse_daily_limit_reset
    old = ('Response: {"code":6004,"msg":"您的使用量已超出频率限制，将在 2020-01-01 00:00:00 '
           'UTC+8 重置，您也可以切换其他模型继续使用。"}')
    assert parse_daily_limit_reset(old) is None


def test_parse_reset_no_timestamp_returns_none():
    from server.core.quota_limit import parse_daily_limit_reset
    assert parse_daily_limit_reset('{"code":6004,"msg":"quota exceeded"}') is None


def test_parse_reset_no_tz_defaults_utc8():
    """无时区后缀 → 按 UTC+8 假定（上游 CN/Intl 均北京时间下发）。"""
    from server.core.quota_limit import parse_daily_limit_reset
    text = 'Response: {"code":6004,"msg":"将在 2099-01-02 08:00:00 重置，您也可以切换其他模型继续使用。"}'
    got = parse_daily_limit_reset(text)
    assert got == datetime(2099, 1, 2, 0, 0, 0)


def test_daily_limit_reset_or_default_fallback():
    """解析不出 → 现在 + default（下限 60s）。"""
    from server.core.quota_limit import daily_limit_reset_or_default
    got = daily_limit_reset_or_default('{"code":6004,"msg":"quota exceeded"}', default_seconds=3600)
    delta = (got - datetime.utcnow()).total_seconds()
    assert 3500 < delta <= 3600


# ── 3) 记账：note_request_result 写限额记录而非账号冷却 ──────────

async def _db(tmp_path, tables):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    async with engine.begin() as conn:
        for t in tables:
            await conn.run_sync(t.create)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _setup(tmp_path):
    engine, Session = await _db(tmp_path, [OAuthToken.__table__, QuotaLimit.__table__])
    async with Session() as db:
        db.add_all([
            OAuthToken(provider_code="codebuddy_cn", owner="acct-a",
                       access_token_enc="enc-tok-a", is_active=True),
            OAuthToken(provider_code="codebuddy_cn", owner="acct-b",
                       access_token_enc="enc-tok-b", is_active=True),
        ])
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_note_quota_limit_records_model_not_account_cooldown(tmp_path, monkeypatch):
    """限额文案 + model_id → 写 quota_limits；账号 fail_count/cooldown 保持不动。"""
    import server.core.oauth_client as oc
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    msg = _utc8_future_msg(8)
    try:
        await client.note_request_result("codebuddy_cn", "acct-a", ok=False,
                                         status_code=429, error=msg,
                                         model_id="step-5-preview", provider_id=7)
        async with Session() as db:
            tok = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
            # 账号本身不冷却（其他模型仍可用）
            assert tok.fail_count == 0 and tok.cooldown_until is None
            q = (await db.execute(select(QuotaLimit))).scalar_one()
            assert q.provider_code == "codebuddy_cn" and q.owner == "acct-a"
            assert q.model_id == "step-5-preview"
            assert q.provider_id == 7
            assert abs((q.reset_at - _expected_reset(8)).total_seconds()) <= 2
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_cn", "acct-a"))
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_quota_limit_upsert_keeps_later_reset(tmp_path, monkeypatch):
    """同 (账号, 模型) 重复限额 → 单条记录，取更晚的重置时间。"""
    import server.core.oauth_client as oc
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    try:
        await client.note_request_result("codebuddy_cn", "acct-a", ok=False,
                                         status_code=429, error=_utc8_future_msg(5),
                                         model_id="m1", provider_id=7)
        await client.note_request_result("codebuddy_cn", "acct-a", ok=False,
                                         status_code=429, error=_utc8_future_msg(30),
                                         model_id="m1", provider_id=7)
        async with Session() as db:
            rows = (await db.execute(select(QuotaLimit))).scalars().all()
            assert len(rows) == 1
            assert abs((rows[0].reset_at - _expected_reset(30)).total_seconds()) <= 2
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_cn", "acct-a"))
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_success_clears_same_model_limit(tmp_path, monkeypatch):
    """该账号在限额模型上成功 → 清同模型限额记录（提前恢复）。"""
    import server.core.oauth_client as oc
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    try:
        await client.note_request_result("codebuddy_cn", "acct-a", ok=False,
                                         status_code=429, error=_utc8_future_msg(8),
                                         model_id="m1", provider_id=7)
        # 另一模型的限额记录不该被 m2 的成功清掉
        await client.note_request_result("codebuddy_cn", "acct-a", ok=False,
                                         status_code=429, error=_utc8_future_msg(8),
                                         model_id="m2", provider_id=7)
        await client.note_request_result("codebuddy_cn", "acct-a", ok=True, model_id="m1")
        async with Session() as db:
            rows = (await db.execute(select(QuotaLimit))).scalars().all()
            assert [r.model_id for r in rows] == ["m2"]
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_cn", "acct-a"))
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_without_model_id_keeps_old_cooldown_semantics(tmp_path, monkeypatch):
    """不带 model_id 的限额文案 → 旧语义（账号冷却）兜底，不丢记账。"""
    import server.core.oauth_client as oc
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    try:
        await client.note_request_result("codebuddy_cn", "acct-a", ok=False,
                                         status_code=429, error=_utc8_future_msg(8))
        async with Session() as db:
            tok = (await db.execute(
                select(OAuthToken).where(OAuthToken.owner == "acct-a"))).scalar_one()
            assert tok.fail_count == 1 and tok.cooldown_until is not None
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_cn", "acct-a"))
        await engine.dispose()


# ── 4) 选号跳过 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_skips_quota_limited_account_for_that_model(tmp_path, monkeypatch):
    """acct-a 在 m1 上限额 → m1 选号落 acct-b；acct-a 对 m2 仍可选。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            # 直接写限额记录（模拟 m1 上 acct-a 已限额；reset_at 取未来）
            db.add(QuotaLimit(provider_code="codebuddy_cn", owner="acct-a",
                              model_id="m1", reset_at=datetime.utcnow() + timedelta(hours=5)))
            await db.commit()
        async with Session() as db:
            got = await client.pick_access_token_ex("codebuddy_cn", db, model_id="m1")
            assert got == ("acct-b", "tok-b")
            # 其他模型不受影响：acct-a 仍可用（按 id 排序在前）
            got2 = await client.pick_access_token_ex("codebuddy_cn", db, model_id="m2")
            assert got2 is not None and got2[0] == "acct-a"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pick_returns_none_when_all_accounts_limited_for_model(tmp_path, monkeypatch):
    """全部账号在该模型上限额 → 选号返回 None（resolver 转 quota_limited 错误）。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            db.add_all([
                QuotaLimit(provider_code="codebuddy_cn", owner="acct-a",
                           model_id="m1", reset_at=datetime.utcnow() + timedelta(hours=5)),
                QuotaLimit(provider_code="codebuddy_cn", owner="acct-b",
                           model_id="m1", reset_at=datetime.utcnow() + timedelta(hours=6)),
            ])
            await db.commit()
        async with Session() as db:
            assert await client.pick_access_token_ex("codebuddy_cn", db, model_id="m1") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pick_ignores_expired_limit(tmp_path, monkeypatch):
    """reset_at 已过期 → 记录自动失效（查询侧过滤），不阻塞选号。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            db.add(QuotaLimit(provider_code="codebuddy_cn", owner="acct-a",
                              model_id="m1", reset_at=datetime.utcnow() - timedelta(minutes=1)))
            await db.commit()
        async with Session() as db:
            got = await client.pick_access_token_ex("codebuddy_cn", db, model_id="m1")
            assert got is not None
    finally:
        await engine.dispose()


# ── 5) 列表接口聚合 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_connections_includes_quota_limits(tmp_path, monkeypatch):
    """OAuth 连接列表附 quota_limits（仅未过期）。"""
    import server.core.oauth_client as oc
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    try:
        await client.note_request_result("codebuddy_cn", "acct-a", ok=False,
                                         status_code=429, error=_utc8_future_msg(8),
                                         model_id="m1", provider_id=7)
        async with Session() as db:
            conns = await client.list_connections(db)
        by_owner = {c["owner"]: c for c in conns}
        assert [q["model_id"] for q in by_owner["acct-a"]["quota_limits"]] == ["m1"]
        assert by_owner["acct-a"]["quota_limits"][0]["reset_at"].endswith("Z")
        assert by_owner["acct-b"]["quota_limits"] == []
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_cn", "acct-a"))
        await engine.dispose()


# ── 6) 选号/记账的完整闭环：限额后换号成功 ────────────────────────

@pytest.mark.asyncio
async def test_end_to_end_limit_then_switch_account(tmp_path, monkeypatch):
    """acct-a 限额 m1 → 再选 m1 落 acct-b；acct-b 成功不误清 acct-a 的记录。"""
    import server.core.oauth_client as oc
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    monkeypatch.setattr("server.db.AsyncSessionLocal", Session)
    try:
        await client.note_request_result("codebuddy_cn", "acct-a", ok=False,
                                         status_code=429, error=_utc8_future_msg(8),
                                         model_id="m1", provider_id=7)
        async with Session() as db:
            got = await client.pick_access_token_ex("codebuddy_cn", db, model_id="m1")
            assert got == ("acct-b", "tok-b")
        await client.note_request_result("codebuddy_cn", "acct-b", ok=True, model_id="m1")
        async with Session() as db:
            rows = (await db.execute(select(QuotaLimit))).scalars().all()
            assert [(r.owner, r.model_id) for r in rows] == [("acct-a", "m1")]
    finally:
        oc._ACCOUNT_DIRTY.discard(("codebuddy_cn", "acct-a"))
        oc._ACCOUNT_DIRTY.discard(("codebuddy_cn", "acct-b"))
        await engine.dispose()


# ── 7) resolver 层：全账号限额 → quota_limited 错误（不误报未连接） ──

@pytest.mark.asyncio
async def test_resolver_reports_quota_limited_when_all_accounts_limited(tmp_path, monkeypatch):
    from server.core.credential_resolver import resolve_credential_async
    from server.core.oauth_client import get_oauth_client
    from server.models.provider import Provider
    from server.models.model import Model
    engine, Session = await _db(
        tmp_path, [OAuthToken.__table__, QuotaLimit.__table__,
                   Provider.__table__, Model.__table__])
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            db.add_all([
                OAuthToken(provider_code="codebuddy_cn", owner="acct-a",
                           access_token_enc="enc-tok-a", is_active=True),
                OAuthToken(provider_code="codebuddy_cn", owner="acct-b",
                           access_token_enc="enc-tok-b", is_active=True),
                QuotaLimit(provider_code="codebuddy_cn", owner="acct-a",
                           model_id="m1", reset_at=datetime.utcnow() + timedelta(hours=5)),
                QuotaLimit(provider_code="codebuddy_cn", owner="acct-b",
                           model_id="m1", reset_at=datetime.utcnow() + timedelta(hours=6)),
            ])
            await db.commit()
            prov = Provider(name="CodeBuddy CN", base_url="https://copilot.tencent.com/v2",
                            api_type="openai_compat", credential_type="oauth",
                            oauth_code="codebuddy_cn", enabled=True)
            db.add(prov)
            await db.commit()
            mdl = Model(provider_id=prov.id, model_id="m1", display_name="m1",
                        input_price=0, output_price=0, is_free=False, auto_enabled=True,
                        enabled=True, supports_streaming=True, context_length=128000)
            db.add(mdl)
            await db.commit()
            rc = await resolve_credential_async(prov, mdl, db)
            assert not rc.ok
            assert "quota_limited" in (rc.error or ""), rc.error
            assert "resets at" in (rc.error or "")
    finally:
        await engine.dispose()
