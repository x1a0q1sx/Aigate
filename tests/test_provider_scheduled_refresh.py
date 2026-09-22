# -*- coding: utf-8 -*-
"""v4.2 两项能力：
1) 按服务商定时模型刷新（开关+自定义频率、next_at 维护、全局批量互斥）
2) OAuth 多账号路由寻址（oauth_owner 点名；__default 缺失自动兜底）
"""
import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.base import Base
from server.models.provider import Provider
from server.models.oauth_token import OAuthToken


async def _db(tmp_path, tables):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _prov(name, **kw):
    return Provider(name=name, base_url="https://x.invalid/v1", api_type="openai_compat",
                    credential_type="api_key", **kw)


# ── 1) 服务商定时刷新字段 ───────────────────────────────

@pytest.mark.asyncio
async def test_update_provider_sets_schedule(tmp_path):
    from server.api.admin_router import update_provider
    from server.schemas.provider import ProviderUpdate
    engine, Session = await _db(tmp_path, [Provider.__table__])
    try:
        async with Session() as db:
            p = _prov("A")
            p.model_refresh_enabled = False
            db.add(p)
            await db.commit()
            pid = p.id
            r = await update_provider(pid, ProviderUpdate(
                model_refresh_enabled=True, model_refresh_interval_minutes=30), db)
            assert r.model_refresh_enabled is True
            assert r.model_refresh_interval_minutes == 30
            assert r.model_refresh_next_at is not None
            # 频率下限钳制（<5 分钟拉取会打爆上游）
            r2 = await update_provider(pid, ProviderUpdate(
                model_refresh_interval_minutes=1), db)
            assert r2.model_refresh_interval_minutes == 5
            # 仅关闭时 next_at 保留（tick 只扫 enabled 行，无害）
            next_before = r2.model_refresh_next_at
            r3 = await update_provider(pid, ProviderUpdate(model_refresh_enabled=False), db)
            assert r3.model_refresh_enabled is False
            assert r3.model_refresh_next_at == next_before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_provider_with_schedule(tmp_path):
    from server.api.admin_router import create_provider
    from server.schemas.provider import ProviderCreate
    engine, Session = await _db(tmp_path, [Provider.__table__])
    try:
        async with Session() as db:
            r = await create_provider(ProviderCreate(
                name="B", base_url="https://x.invalid/v1",
                model_refresh_enabled=True, model_refresh_interval_minutes=120), db)
            assert r.model_refresh_enabled is True
            assert r.model_refresh_interval_minutes == 120
            assert r.model_refresh_next_at > datetime.utcnow()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_global_scheduled_skips_per_provider(tmp_path, monkeypatch):
    """全局 scheduled 批量必须排除已单独设频的服务商（避免双刷）。"""
    import server.api.admin_router as ar
    engine, Session = await _db(tmp_path, [Provider.__table__])
    called = []

    async def _fake_refresh(db, provider, key_manager, trigger=None):
        called.append(provider.name)
        return {"added": 0, "updated": 0, "total": 0, "removed": 0,
                "pricing_updated": 0, "metric_updated": 0}

    monkeypatch.setattr(ar._model_catalog, "refresh_models_from_provider", _fake_refresh)
    try:
        async with Session() as db:
            db.add(_prov("solo"))                      # 常规：走全局批量
            p2 = _prov("own-schedule")
            db.add(p2)
            await db.commit()
            p2.model_refresh_enabled = True
            p2.model_refresh_interval_minutes = 30
            await db.commit()
            await ar.refresh_models(None, "scheduled", db)
        assert called == ["solo"]
        # 指定 provider_id 刷新（tick 的调用方式）不受排除逻辑影响
        async with Session() as db:
            called.clear()
            rows = (await db.execute(select(Provider).where(Provider.name == "own-schedule"))).scalars().all()
            await ar.refresh_models(rows[0].id, "scheduled", db)
        assert called == ["own-schedule"]
    finally:
        await engine.dispose()


# ── 2) OAuth 多账号寻址 ─────────────────────────────────

class _FakeCrypto:
    def encrypt(self, s): return "enc-" + s
    def decrypt(self, s): return (s[4:] if s.startswith("enc-") else s)


async def _oauth_setup(tmp_path):
    engine, Session = await _db(tmp_path, [OAuthToken.__table__])
    async with Session() as db:
        db.add_all([
            OAuthToken(provider_code="codebuddy_cn", owner="13800000000",
                       access_token_enc="enc-tok-phone", is_active=True),
            OAuthToken(provider_code="codebuddy_cn", owner="13900000000",
                       access_token_enc="enc-tok-phone2", is_active=True),
            OAuthToken(provider_code="cline", owner="disabled-one",
                       access_token_enc="enc-tok-off", is_active=False),
        ])
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_pick_falls_back_when_default_missing(tmp_path, monkeypatch):
    """主账号被改名（无 __default 行）→ 自动兜底任一 active 连接（回归实测踩坑）"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _oauth_setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            tok = await client.pick_access_token("codebuddy_cn", db)
            assert tok == "tok-phone"  # id 最早的一条
            # 点名存在的账号 → 精确取号
            tok2 = await client.pick_access_token("codebuddy_cn", db, owner="13900000000")
            assert tok2 == "tok-phone2"
            # 点名不存在/已停用的账号 → 不静默换号，返回 None
            assert await client.pick_access_token("codebuddy_cn", db, owner="ghost") is None
            assert await client.pick_access_token("cline", db, owner="disabled-one") is None
            # 自动模式对 cline：唯一连接已停用 → 兜底也找不到 active → None
            assert await client.pick_access_token("cline", db) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pick_prefers_default_row(tmp_path, monkeypatch):
    """__default 存在时优先用它，兜底逻辑不得抢主账号"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _db(tmp_path, [OAuthToken.__table__])
    async with Session() as db:
        db.add(OAuthToken(provider_code="x", owner="first", access_token_enc="enc-a", is_active=True))
        db.add(OAuthToken(provider_code="x", owner="__default", access_token_enc="enc-main", is_active=True))
        await db.commit()
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            assert await client.pick_access_token("x", db) == "main"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolver_passes_oauth_owner(tmp_path, monkeypatch):
    """credential_resolver 必须把 provider.oauth_owner 透传给 pick_access_token"""
    import server.core.oauth_client as oc_mod
    captured = {}

    class _Spy:
        async def pick_access_token(self, code, db, owner="__default"):
            captured["owner"] = owner
            return "tok"

    monkeypatch.setattr(oc_mod, "get_oauth_client", lambda: _Spy())
    # _merge_oauth_headers 走真实实现需要 provider 字段，给个轻量替身
    import server.api.v1_router as vr
    monkeypatch.setattr(vr, "_merge_oauth_headers", lambda p, h: {"__oauth": True})
    from server.core.credential_resolver import resolve_credential_async
    provider = SimpleNamespace(id=1, name="CodeBuddy A", base_url="https://x.invalid",
                               api_type="openai_compat", credential_type="oauth",
                               oauth_code="codebuddy_cn", oauth_owner="13800000000",
                               headers=None, proxy_enabled=False)
    rc = await resolve_credential_async(provider, SimpleNamespace(model_id="m"), None)
    assert rc.ok and captured["owner"] == "13800000000"
    # 未点名 → 自动 __default
    provider.oauth_owner = None
    await resolve_credential_async(provider, SimpleNamespace(model_id="m"), None)
    assert captured["owner"] == "__default"
