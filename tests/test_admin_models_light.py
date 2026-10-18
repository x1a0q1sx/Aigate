# -*- coding: utf-8 -*-
"""组合路由页轻量模型列表：GET /admin/api/models/light。

背景：/admin/api/models 全量目录 ~2.4MB/476ms，组合页只用少量字段，
整包拉取是该页秒开慢的主因之一。

2026-10-18 增补 `schedule_blocked`：组合页需要显示「时段外」徽标
（v4.6 生效时段：时段外的候选在请求时会被跳过）。
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
            # 生效时段配置为「仅时段内可用」且窗口极窄（不可能命中）→ 时段外
            Model(provider_id=p2.id, model_id="night-only", display_name="Night",
                  is_free=True, input_price=0, output_price=0, enabled=True,
                  active_schedule={
                      "mode": "only", "timezone": "Asia/Shanghai",
                      "rules": [{"start": "03:00", "end": "03:01", "days": [],
                                 "date_from": "", "date_to": "", "label": "夜间"}],
                  }),
        ])
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_light_returns_expected_fields(tmp_path):
    engine, Session = await _seed(tmp_path)
    try:
        async with Session() as db:
            items = await list_models_light(db)
        assert len(items) == 4
        expected_keys = {"id", "provider_id", "model_id", "display_name",
                         "is_free", "input_price", "output_price",
                         "avg_latency_ms", "enabled", "schedule_blocked"}
        for it in items:
            assert set(it.keys()) == expected_keys

        by_key = {(i["provider_id"], i["model_id"]): i for i in items}
        free = next(i for i in items if i["model_id"] == "b-model")
        assert free["avg_latency_ms"] == 123.4
        assert free["enabled"] is True
        paid = next(i for i in items if i["model_id"] == "a-model")
        assert paid["input_price"] == 1.5 and paid["output_price"] == 3.0
        # 未配置时段的模型恒 False
        assert free["schedule_blocked"] is False
        # 序列化后可直接 JSON 返回（无 ORM 对象残留）
        import json
        json.dumps(items, ensure_ascii=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_light_marks_schedule_blocked(tmp_path):
    """配置了「仅时段内」且当前不在窗口内 → schedule_blocked=True（组合页徽标）。"""
    engine, Session = await _seed(tmp_path)
    try:
        async with Session() as db:
            items = await list_models_light(db)
        night = next(i for i in items if i["model_id"] == "night-only")
        # 03:00-03:01 的窗口：除这一分钟外恒为时段外（测试运行时刻几乎必然命中）
        from datetime import datetime
        from server.core.model_schedule import evaluate
        now_active = evaluate({
            "mode": "only", "timezone": "Asia/Shanghai",
            "rules": [{"start": "03:00", "end": "03:01", "days": [],
                       "date_from": "", "date_to": "", "label": "夜间"}],
        }, now=datetime.utcnow())["active"]
        assert night["schedule_blocked"] is (not now_active)
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
