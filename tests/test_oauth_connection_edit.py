# -*- coding: utf-8 -*-
"""OAuth 连接管理：账号改名（owner 唯一性）+ 新账号自动分配（不覆盖主账号）"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.base import Base
from server.models.oauth_token import OAuthToken
from server.api.oauth_router import (
    update_connection, ConnectionUpdate, _next_owner_for_provider,
)


async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/o.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[OAuthToken.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        db.add_all([
            OAuthToken(provider_code="codebuddy_cn", owner="__default",
                       access_token_enc="enc-a", is_active=True),
            OAuthToken(provider_code="codebuddy_cn", owner="account-2",
                       access_token_enc="enc-b", is_active=True),
            OAuthToken(provider_code="qoder", owner="__default",
                       access_token_enc="enc-c", is_active=True),
        ])
        await db.commit()
        rows = (await db.execute(select(OAuthToken).order_by(OAuthToken.id))).scalars().all()
        ids = {r.owner: r.id for r in rows}
    return engine, Session, ids


@pytest.mark.asyncio
async def test_rename_owner_ok(tmp_path):
    engine, Session, ids = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await update_connection(ids["account-2"], ConnectionUpdate(owner="工作号"), db)
            assert r["ok"] and r["owner"] == "工作号"
            row = await db.get(OAuthToken, ids["account-2"])
            assert row.owner == "工作号"
            # 主账号未受影响
            assert (await db.get(OAuthToken, ids["__default"])).owner == "__default"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rename_duplicate_rejected(tmp_path):
    """同服务商内重名必须 409（否则两条连接会互相覆盖 token）"""
    engine, Session, ids = await _setup(tmp_path)
    try:
        async with Session() as db:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as ei:
                await update_connection(ids["account-2"],
                                        ConnectionUpdate(owner="__default"), db)
            assert ei.value.status_code == 409
            # 原值未变
            assert (await db.get(OAuthToken, ids["account-2"])).owner == "account-2"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rename_cross_provider_allowed(tmp_path):
    """不同服务商可同名（寻址键是 provider_code+owner）"""
    engine, Session, ids = await _setup(tmp_path)
    try:
        async with Session() as db:
            # qoder 的 __default 改名为 codebuddy_cn 也有的名字「account-2」→ 应放行
            r = await update_connection(ids["__default"], ConnectionUpdate(owner="account-2"), db)
            assert r["ok"] and r["owner"] == "account-2"
            # qoder 下现在只有改名后的这条，与 codebuddy_cn 的两条互不影响
            rows = (await db.execute(select(OAuthToken).where(
                OAuthToken.owner == "account-2"))).scalars().all()
            assert {r.provider_code for r in rows} == {"codebuddy_cn", "qoder"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rename_empty_rejected(tmp_path):
    engine, Session, ids = await _setup(tmp_path)
    try:
        async with Session() as db:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as ei:
                await update_connection(ids["account-2"], ConnectionUpdate(owner="   "), db)
            assert ei.value.status_code == 400
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_toggle_active(tmp_path):
    engine, Session, ids = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await update_connection(ids["account-2"], ConnectionUpdate(is_active=False), db)
            assert r["is_active"] is False
            assert (await db.get(OAuthToken, ids["account-2"])).is_active is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_next_owner_allocation(tmp_path):
    """新增账号名分配：首个 __default，其后 account-N 递增，不覆盖已有"""
    engine, Session, ids = await _setup(tmp_path)
    try:
        async with Session() as db:
            # codebuddy_cn 已有 __default + account-2 → 下一个是 account-3
            assert await _next_owner_for_provider(db, "codebuddy_cn") == "account-3"
            # 全新服务商 → __default
            assert await _next_owner_for_provider(db, "fresh_provider") == "__default"
            # 只有 account-2（无主账号）→ 补 __default
            db.add(OAuthToken(provider_code="only-second", owner="account-2",
                              access_token_enc="enc-x", is_active=True))
            await db.commit()
            assert await _next_owner_for_provider(db, "only-second") == "__default"
    finally:
        await engine.dispose()
