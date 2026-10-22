# -*- coding: utf-8 -*-
"""请求日志筛选（2026-10-20）：服务商 / 模型 / 账号三路筛选 + 候选值接口。

背景：分析页日志区原来只有一个「全部服务商」原生下拉，模型和账号无法筛选，
服务商多了以后下拉里翻找也很费劲。现在三路都换成可输入搜索的筛选框，
后端相应支持 model / account 子串筛选，并提供 /logs/filters 给候选值。

口径约定：
- provider 精确匹配（候选值来自日志本身，选中即全等）
- model / account 子串匹配且忽略大小写（用户通常只记得片段）
- 三路条件叠加为 AND
"""
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api import admin_routing
from server.models.base import Base
from server.models.model import Model
from server.models.provider import Provider
from server.models.request_log import RequestLog


async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lf.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[
            Provider.__table__, Model.__table__, RequestLog.__table__,
        ])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        db.add_all([
            RequestLog(requested_model="auto", routed_provider="Alpha", routed_model="kimi-k3",
                       routed_account="a@x.com", status="success",
                       created_at=datetime(2026, 9, 28, 8, 0), is_health_check=False),
            RequestLog(requested_model="auto", routed_provider="Alpha", routed_model="gpt-5.5",
                       routed_account="b@x.com", status="error",
                       created_at=datetime(2026, 9, 28, 9, 0), is_health_check=False),
            RequestLog(requested_model="auto", routed_provider="Beta", routed_model="kimi-k3",
                       status="success",
                       created_at=datetime(2026, 9, 28, 10, 0), is_health_check=False),
            # 健康检查行永远不出现在日志列表里
            RequestLog(requested_model="auto", routed_provider="Alpha", routed_model="kimi-k3",
                       status="success", created_at=datetime(2026, 9, 28, 11, 0),
                       is_health_check=True),
        ])
        await db.commit()
    return engine, Session


async def _list(db, **kw):
    kw.setdefault("page", 1)
    kw.setdefault("page_size", 50)
    kw.setdefault("status", None)
    kw.setdefault("provider", None)
    kw.setdefault("model", None)
    kw.setdefault("account", None)
    kw.setdefault("log_type", None)
    return await admin_routing.list_request_logs(db=db, **kw)


@pytest.mark.asyncio
async def test_filter_by_model_substring_ignore_case(tmp_path):
    """模型筛选是子串 + 忽略大小写：搜 KIMI 也能命中 kimi-k3"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await _list(db, model="KIMI")
        assert r["total"] == 2                       # Alpha + Beta 各一条（健康检查行排除）
        assert {i["routed_model"] for i in r["items"]} == {"kimi-k3"}
        async with Session() as db:
            r2 = await _list(db, model="5.5")
        assert r2["total"] == 1 and r2["items"][0]["routed_provider"] == "Alpha"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_filter_by_account_and_combined(tmp_path):
    """账号筛选 + 与服务商/状态叠加为 AND"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await _list(db, account="a@x")
        assert r["total"] == 1
        assert r["items"][0]["routed_account"] == "a@x.com"
        # 三路叠加：Alpha + kimi-k3 + b@x.com 无交集
        async with Session() as db:
            r2 = await _list(db, provider="Alpha", model="kimi-k3", account="b@x.com")
        assert r2["total"] == 0
        # 服务商 + 模型：Alpha 下的 kimi-k3
        async with Session() as db:
            r3 = await _list(db, provider="Alpha", model="kimi")
        assert r3["total"] == 1
        assert r3["items"][0]["routed_account"] == "a@x.com"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_total_cache_key_separates_filters(tmp_path):
    """total 缓存键必须带上新维度，否则换筛选条件会命中上一个条件的总数"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            a = await _list(db, model="kimi")
            b = await _list(db, model="gpt")
        assert a["total"] == 2
        assert b["total"] == 1          # 若缓存键漏了 model 会错误返回 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_log_filters_endpoint_lists_distinct_values(tmp_path):
    """/logs/filters 给出三路候选值（去重、排序、跳过空值、排除健康检查之外的重复项）"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            d = await admin_routing.list_log_filters(db=db)
        assert d["providers"] == ["Alpha", "Beta"]
        assert d["models"] == ["gpt-5.5", "kimi-k3"]
        assert d["accounts"] == ["a@x.com", "b@x.com"]   # Beta 那条没有账号 → 不出现空串
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_logs_export_accepts_model_and_account(tmp_path):
    """导出跟随筛选：model / account 参数同样生效（与屏幕上看到的一致）"""
    from server.api import admin_ops_router
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            resp = await admin_ops_router.export_logs(
                format="json", hours=24 * 365 * 10, status=None, provider=None,
                model="gpt", account="b@x", db=db)
            body = b""
            async for chunk in resp.body_iterator:
                body += chunk if isinstance(chunk, bytes) else chunk.encode()
        import json as _json
        rows = _json.loads(body.decode())
        assert len(rows) == 1
        assert rows[0]["routed_model"] == "gpt-5.5"
        assert rows[0]["routed_account"] == "b@x.com"   # 导出新增账号列
    finally:
        await engine.dispose()


def test_log_detail_route_does_not_shadow_export():
    """`/logs/{log_id}` 必须限定整数，否则它会把 `/logs/export` 当成 id=export → 422。

    这条路由注册在 admin_routing，而 /logs/export 在 admin_ops（更晚 include），
    Starlette 按注册顺序匹配，所以路径转换器是唯一的分流手段。
    """
    import inspect
    from server.api import admin_routing
    src = inspect.getsource(admin_routing)
    assert '@router.get("/logs/{log_id:int}")' in src
    assert '@router.get("/logs/{log_id}")' not in src


def test_logs_export_route_not_shadowed_by_detail_route():
    """`/logs/{log_id}` 必须限定整数，否则 `/logs/export` 会被当成 id=export → 422。

    这条详情路由注册在 admin_routing，而 /logs/export 在 admin_ops（更晚 include），
    Starlette 按注册顺序匹配，路径转换器是唯一的分流手段。此处直接检查路由表，
    不走网络（ASGI 层会真实执行 DB 依赖，不适合只验证匹配顺序）。
    """
    import inspect
    from server.api import admin_routing

    src = inspect.getsource(admin_routing)
    assert '@router.get("/logs/{log_id:int}")' in src
    assert '@router.get("/logs/{log_id}")' not in src

    # admin_ops 的导出路由本身仍存在（router 带 /admin/api 前缀）
    from server.api import admin_ops_router
    export_paths = [r.path for r in admin_ops_router.router.routes
                    if getattr(r, "path", "") == "/admin/api/logs/export"]
    assert export_paths == ["/admin/api/logs/export"]
