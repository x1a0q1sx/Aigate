# -*- coding: utf-8 -*-
"""组合路由页轻量模型列表：GET /admin/api/models/light（9 字段）。

背景：/admin/api/models 全量目录 ~2.4MB/476ms，组合页只用 9 个字段，
整包拉取是该页秒开慢的主因之一。
"""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.base import Base
from server.models.provider import Provider
from server.models.model import Model
from server.api.admin_router import list_models_light


async def _seed(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/g.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[Provider.__table__, Model.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        p1 = Provider(name="p1", base_url="https://x/v1")
        p2 = Provider(name="p2", base_url="https://y/v1")
        db.add_all([p1, p2])
        await db.flush()
        db.add_all([
            Model(provider_id=p1.id, model_id="b-model", display_name="B",
                  is_free=True, input_price=0, output_price=0,
                  avg_latency_ms=123.4, enabled=True),
            Model(provider_id=p1.id, model_id="a-model", display_name="A",
                  is_free=False, input_price=1.5, output_price=3.0,
                  enabled=False),
            Model(provider_id=p2.id, model_id="z-model", display_name="Z",
                  is_free=False, input_price=2.0, output_price=4.0,
                  enabled=True),
        ])
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_light_returns_only_nine_fields(tmp_path):
    engine, Session = await _seed(tmp_path)
    try:
        async with Session() as db:
            items = await list_models_light(db)
        assert len(items) == 3
        expected_keys = {"id", "provider_id", "model_id", "display_name",
                         "is_free", "input_price", "output_price",
                         "avg_latency_ms", "enabled"}
        for it in items:
            assert set(it.keys()) == expected_keys

        by_key = {(i["provider_id"], i["model_id"]): i for i in items}
        free = next(i for i in items if i["is_free"])
        assert free["avg_latency_ms"] == 123.4
        assert free["enabled"] is True
        paid = next(i for i in items if not i["is_free"] and not i["enabled"])
        assert paid["input_price"] == 1.5 and paid["output_price"] == 3.0
        # 序列化后可直接 JSON 返回（无 ORM 对象残留）
        import json
        json.dumps(items, ensure_ascii=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_light_ordered_by_provider_then_model(tmp_path):
    engine, Session = await _seed(tmp_path)
    try:
        async with Session() as db:
            items = await list_models_light(db)
        keys = [(i["provider_id"], i["model_id"]) for i in items]
        assert keys == sorted(keys)
    finally:
        await engine.dispose()
