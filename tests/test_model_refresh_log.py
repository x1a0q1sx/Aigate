# -*- coding: utf-8 -*-
"""模型刷新日志：refresh_models_from_provider 计时落库 + /logs?log_type=refresh 查询。"""
import json
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.base import Base
from server.models.provider import Provider
from server.models.model_refresh_log import ModelRefreshLog
from server.models.request_log import RequestLog
from server.core.model_catalog import ModelCatalog


async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/r.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[Provider.__table__, ModelRefreshLog.__table__, RequestLog.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        p = Provider(name="p1", base_url="https://x/v1", enabled=True)
        db.add(p)
        await db.commit()
        pid = p.id
    return engine, Session, pid


@pytest.mark.asyncio
async def test_success_and_pricing_error_logged(tmp_path, monkeypatch):
    engine, Session, pid = await _setup(tmp_path)
    cat = ModelCatalog()

    async def fake_inner(self, session, provider, key_manager):
        return {"added": 2, "updated": 1, "removed": 0, "total": 10,
                "added_models": [{"model_id": "a"}, {"model_id": "b"}],
                "removed_models": [], "pricing_updated": 3, "metric_updated": 1,
                "pricing_source": "https://price", "pricing_error": "timeout"}

    monkeypatch.setattr(ModelCatalog, "_refresh_models_inner", fake_inner)
    try:
        async with Session() as db:
            p = await db.get(Provider, pid)
            r = await cat.refresh_models_from_provider(db, p, None, trigger="scheduled")
            assert r["added"] == 2
        async with Session() as db:
            rows = (await db.execute(select(ModelRefreshLog))).scalars().all()
            assert len(rows) == 1
            row = rows[0]
            assert row.ok is True
            assert row.trigger == "scheduled"
            assert (row.added, row.updated, row.total, row.pricing_updated) == (2, 1, 10, 3)
            assert json.loads(row.added_models) == [{"model_id": "a"}, {"model_id": "b"}]
            # 非致命定价错误也进 error 列（但行仍算成功）
            assert "timeout" in (row.error or "")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_error_dict_and_exception_logged(tmp_path, monkeypatch):
    engine, Session, pid = await _setup(tmp_path)
    cat = ModelCatalog()

    async def err_inner(self, session, provider, key_manager):
        return {"error": "boom"}

    monkeypatch.setattr(ModelCatalog, "_refresh_models_inner", err_inner)
    async with Session() as db:
        p = await db.get(Provider, pid)
        r = await cat.refresh_models_from_provider(db, p, None)
        assert r["error"] == "boom"

    async def raise_inner(self, session, provider, key_manager):
        raise RuntimeError("net down")

    monkeypatch.setattr(ModelCatalog, "_refresh_models_inner", raise_inner)
    async with Session() as db:
        p = await db.get(Provider, pid)
        with pytest.raises(RuntimeError):
            await cat.refresh_models_from_provider(db, p, None)
    async with Session() as db:
        rows = (await db.execute(select(ModelRefreshLog).order_by(ModelRefreshLog.id))).scalars().all()
        assert len(rows) == 2
        assert rows[0].ok is False and rows[0].error == "boom" and rows[0].trigger == "manual"
        assert rows[1].ok is False and "net down" in rows[1].error
    await engine.dispose()


@pytest.mark.asyncio
async def test_logs_endpoint_refresh_type(tmp_path):
    engine, Session, pid = await _setup(tmp_path)
    async with Session() as db:
        db.add(ModelRefreshLog(provider_id=pid, provider_name="p1", ok=True, trigger="manual",
                               duration_ms=1234, added=1, updated=2, removed=0, total=9,
                               pricing_updated=0, metric_updated=0,
                               added_models=json.dumps([{"model_id": "a"}]), removed_models="[]"))
        db.add(RequestLog(requested_model="m", routed_provider="p1", routed_model="m",
                          status="success", created_at=datetime(2026, 1, 1),
                          is_health_check=False, prompt_tokens=1, completion_tokens=2))
        await db.commit()
    from server.api.admin_routing import list_request_logs
    async with Session() as db:
        d = await list_request_logs(page=1, page_size=10, status=None, provider=None,
                                    log_type="refresh", db=db)
        assert d["total"] == 1
        it = d["items"][0]
        assert it["log_type"] == "refresh" and it["status"] == "success"
        assert it["duration_ms"] == 1234 and it["added_models"] == [{"model_id": "a"}]
        # 默认仍是请求日志（向后兼容），且带 log_type 标记
        d2 = await list_request_logs(page=1, page_size=10, status=None, provider=None, db=db)
        assert d2["total"] == 1 and d2["items"][0]["log_type"] == "request"
        # 状态筛选在 refresh 类型下映射 ok
        d3 = await list_request_logs(page=1, page_size=10, status="error", provider=None,
                                     log_type="refresh", db=db)
        assert d3["total"] == 0
        # 服务商筛选
        d4 = await list_request_logs(page=1, page_size=10, status=None, provider="不存在",
                                     log_type="refresh", db=db)
        assert d4["total"] == 0
    await engine.dispose()
