# -*- coding: utf-8 -*-
"""模型分组复选：PUT /admin/api/models/{id}/groups（Auto + 多个 combo 归属）"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.base import Base
from server.models.provider import Provider
from server.models.model import Model
from server.models.combo import Combo
from server.api.admin_router import update_model_groups, ModelGroupsUpdate


async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/g.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[Provider.__table__, Model.__table__, Combo.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        p = Provider(name="p1", base_url="https://x/v1")
        db.add(p)
        await db.flush()
        m = Model(provider_id=p.id, model_id="m-target")
        m2 = Model(provider_id=p.id, model_id="m-other")
        db.add_all([m, m2])
        # 组合 A 为空；组合 B 含目标模型与另一模型（验证不波及其他成员）
        db.add_all([
            Combo(name="A", model_ids=[]),
            Combo(name="B", model_ids=[{"provider": "p1", "model_id": "m-target"},
                                        {"provider": "p1", "model_id": "m-other"}]),
        ])
        await db.commit()
        ids = {"m": m.id, "m2": m2.id}
    return engine, Session, ids


@pytest.mark.asyncio
async def test_set_groups_multi_and_auto(tmp_path):
    engine, Session, ids = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await update_model_groups(ids["m"], ModelGroupsUpdate(auto_enabled=True, combos=["A", "B"]), db)
            assert r["ok"] and r["auto_enabled"] is True
            a = (await db.execute(select(Combo).where(Combo.name == "A"))).scalar_one()
            b = (await db.execute(select(Combo).where(Combo.name == "B"))).scalar_one()
            assert {"provider": "p1", "model_id": "m-target"} in a.model_ids
            assert {"provider": "p1", "model_id": "m-target"} in b.model_ids
            m = (await db.execute(select(Model).where(Model.id == ids["m"]))).scalar_one()
            assert m.auto_enabled is True
        # 取消 B：目标移出，但其他成员保留；combos=None 时分组不动，只改 Auto
        async with Session() as db:
            await update_model_groups(ids["m"], ModelGroupsUpdate(combos=["A"]), db)
            b = (await db.execute(select(Combo).where(Combo.name == "B"))).scalar_one()
            assert b.model_ids == [{"provider": "p1", "model_id": "m-other"}]
        async with Session() as db:
            await update_model_groups(ids["m"], ModelGroupsUpdate(auto_enabled=False), db)
            a = (await db.execute(select(Combo).where(Combo.name == "A"))).scalar_one()
            m = (await db.execute(select(Model).where(Model.id == ids["m"]))).scalar_one()
            assert {"provider": "p1", "model_id": "m-target"} in a.model_ids
            assert m.auto_enabled is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_combo_name_ignored(tmp_path):
    engine, Session, ids = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await update_model_groups(ids["m"], ModelGroupsUpdate(combos=["不存在", "A"]), db)
            assert r["ok"]
            a = (await db.execute(select(Combo).where(Combo.name == "A"))).scalar_one()
            assert any(e.get("model_id") == "m-target" for e in a.model_ids)
    finally:
        await engine.dispose()
