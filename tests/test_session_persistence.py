"""登录会话持久化 + 滑动续期回归测试。

对应问题：session 曾只存进程内存，pm2 每次重启全员掉线
（表现为"刚登录一会儿就过期"）。现在落库 admin_sessions 表，
重启不丢会话；活跃使用在剩余不足 TTL/2 时自动续期。
"""
import asyncio
from datetime import datetime, timedelta

import pytest

import server.core.auth as auth_mod


def _make_db(tmp_path, monkeypatch):
    """tmp sqlite + monkeypatch auth._db_session；返回 (engine, Session)。"""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    db_file = tmp_path / "sessions.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        from server.models.admin_session import AdminSession
        async with engine.begin() as conn:
            await conn.run_sync(AdminSession.__table__.create)

    asyncio.run(_setup())
    monkeypatch.setattr(auth_mod, "_db_session", Session)
    return engine, Session


def _clear_cache():
    auth_mod._sessions.clear()


@pytest.fixture
def session_db(tmp_path, monkeypatch):
    engine, Session = _make_db(tmp_path, monkeypatch)
    _clear_cache()
    yield Session
    asyncio.run(engine.dispose())
    _clear_cache()


def test_create_validate_destroy_persisted(session_db):
    async def _t():
        token = await auth_mod.create_session("admin")
        assert await auth_mod.validate_session(token) is True
        # DB 里有权威行
        from server.models.admin_session import AdminSession
        async with session_db() as db:
            row = await db.get(AdminSession, token)
            assert row is not None and row.username == "admin"
        # 注销后缓存与库都失效
        await auth_mod.destroy_session(token)
        assert await auth_mod.validate_session(token) is False
        async with session_db() as db:
            assert await db.get(AdminSession, token) is None
    asyncio.run(_t())


def test_restart_keeps_login(session_db):
    """模拟服务重启：内存缓存清空后 session 仍有效（回源 DB 并回填缓存）。"""
    async def _t():
        token = await auth_mod.create_session("admin")
        _clear_cache()  # 进程重启
        assert await auth_mod.validate_session(token) is True
        assert token in auth_mod._sessions  # L1 回填
    asyncio.run(_t())


def test_unknown_token_rejected(session_db):
    async def _t():
        assert await auth_mod.validate_session("no-such-token") is False
    asyncio.run(_t())


def test_sliding_renewal_on_activity(session_db):
    """剩余时间不足 TTL/2 时校验成功即滑动续期（持续使用中不中途过期）。"""
    async def _t():
        from server.models.admin_session import AdminSession
        token = await auth_mod.create_session("admin")
        ttl = auth_mod._session_ttl()
        soon = datetime.utcnow() + ttl / 4  # 剩 1/4 TTL < 1/2 TTL
        auth_mod._sessions[token]["expires_at"] = soon
        async with session_db() as db:
            row = await db.get(AdminSession, token)
            row.expires_at = soon
            await db.commit()
        assert await auth_mod.validate_session(token) is True
        # 缓存与 DB 均延长到接近完整 TTL
        assert auth_mod._sessions[token]["expires_at"] > datetime.utcnow() + ttl * 0.9
        async with session_db() as db:
            row = await db.get(AdminSession, token)
            assert row.expires_at > datetime.utcnow() + ttl / 2
    asyncio.run(_t())


def test_expired_session_rejected_and_cleaned(session_db):
    async def _t():
        from server.models.admin_session import AdminSession
        token = await auth_mod.create_session("admin")
        past = datetime.utcnow() - timedelta(minutes=1)
        auth_mod._sessions[token]["expires_at"] = past
        async with session_db() as db:
            row = await db.get(AdminSession, token)
            row.expires_at = past
            await db.commit()
        assert await auth_mod.validate_session(token) is False
        async with session_db() as db:
            assert await db.get(AdminSession, token) is None  # 过期行顺手清除
    asyncio.run(_t())


def test_clear_all_sessions_forces_relogin(session_db):
    async def _t():
        t1 = await auth_mod.create_session("admin")
        t2 = await auth_mod.create_session("admin")
        await auth_mod.clear_all_sessions()
        assert await auth_mod.validate_session(t1) is False
        assert await auth_mod.validate_session(t2) is False
    asyncio.run(_t())


def test_db_down_degrades_to_memory_session(tmp_path, monkeypatch):
    """DB 异常时登录仍可用（退化为内存会话），不至于锁死管理面板。"""
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(auth_mod, "_db_session", _boom)
    _clear_cache()
    try:
        async def _t():
            token = await auth_mod.create_session("admin")
            assert await auth_mod.validate_session(token) is True
        asyncio.run(_t())
    finally:
        _clear_cache()


def test_session_timeout_configured_two_hours():
    assert auth_mod._session_ttl() == timedelta(hours=auth_mod.config.auth.session_timeout_hours)
