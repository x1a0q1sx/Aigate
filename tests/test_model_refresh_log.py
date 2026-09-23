# -*- coding: utf-8 -*-
"""模型刷新日志：refresh_models_from_provider 计时落库 + /logs?log_type=refresh 查询。"""
import json
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.base import Base
from server.models.provider import Provider
from server.models.model import Model
from server.models.model_refresh_log import ModelRefreshLog
from server.models.request_log import RequestLog
from server.core.model_catalog import ModelCatalog


async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/r.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[Provider.__table__, Model.__table__,
                                    ModelRefreshLog.__table__, RequestLog.__table__])
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
        # 来源追踪字段透出（v4.3）：老数据无值 → "unknown"
        assert it["list_source"] == "unknown" and it["list_note"] is None
        # 状态筛选在 refresh 类型下映射 ok
        d3 = await list_request_logs(page=1, page_size=10, status="error", provider=None,
                                     log_type="refresh", db=db)
        assert d3["total"] == 0
        # 服务商筛选
        d4 = await list_request_logs(page=1, page_size=10, status=None, provider="不存在",
                                     log_type="refresh", db=db)
        assert d4["total"] == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_fallback_is_labeled(tmp_path, monkeypatch):
    """v4.3: OAuth 在线列表失败 → 静态种子兜底时，日志必须标 list_source=seed（不再伪装在线拉取）。

    这正是 CodeBuddy 类服务商的处境：无 /v2/models 端点，永远走种子回退。
    """
    engine, Session, pid = await _setup(tmp_path)
    async with Session() as db:
        p = await db.get(Provider, pid)
        p.credential_type = "oauth"
        p.oauth_code = "codebuddy_intl"
        await db.commit()

    from server.core import oauth_client as oc_mod

    class _NoTokenClient:
        async def pick_access_token(self, code, session, owner="__default"):
            return None  # 无 token → 直接走种子兜底

    monkeypatch.setattr(oc_mod, "get_oauth_client", lambda: _NoTokenClient())

    async def _no_pricing(base_url, timeout=None):
        class _R:
            pricing = {}
            source_url = ""
            error = ""
        return _R()

    from server.core import model_catalog as mc
    monkeypatch.setattr(mc, "fetch_provider_pricing", _no_pricing)

    cat = ModelCatalog()
    async with Session() as db:
        p = await db.get(Provider, pid)
        r = await cat.refresh_models_from_provider(db, p, None)
        assert "error" not in r, r
        assert r["list_source"] == "seed"
        assert "种子" in (r["list_note"] or "")
    async with Session() as db:
        row = (await db.execute(select(ModelRefreshLog))).scalars().first()
        assert row.ok is True
        assert row.list_source == "seed"
        assert "种子" in (row.list_note or "")
    await engine.dispose()


@pytest.mark.asyncio
async def test_codebuddy_intl_seed_has_deepseek_v41_flash(tmp_path, monkeypatch):
    """回归：国际版种子必须含群友反馈的 deepseek-v4.1-flash，且剔除已下架的旧名。

    2026-09-23 实测：codebuddy_intl 在线列表不可用（404 无该端点），
    种子是唯一兜底来源——种子错了就表现为「模型列表不对」。
    """
    from server.core.oauth_registry import get_oauth_provider

    intl = get_oauth_provider("codebuddy_intl")
    assert intl is not None
    ids = {m["model_id"] for m in intl.static_models}
    assert "deepseek-v4.1-flash" in ids, "国际版种子缺 deepseek-v4.1-flash"
    assert "gpt-6-astra" in ids and "gemini-3.5-flash" in ids, "国际版独有模型未入种子"
    # 已下架（11102 model service info not found）不得残留
    for gone in ("deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v3-2-volc",
                 "glm-4.7", "minimax-m2.7"):
        assert gone not in ids, f"{gone} 已从上游下架，不应留在国际种子"

    cn = get_oauth_provider("codebuddy_cn")
    cn_ids = {m["model_id"] for m in cn.static_models}
    assert "deepseek-v4.1-flash" in cn_ids and "deepseek-v4-pro" in cn_ids
    assert "glm-5.0" not in cn_ids  # CN 已下架

