# -*- coding: utf-8 -*-
"""按服务商用量聚合修复（2026-09-24）。

生产现象：分析页「按服务商用量」显示不全——当天 832 行日志里只列出 4 家，
而实际有 6 家（CodeBuddy CN 596 行、Qoder 139 行全部缺席）。

根因：`routed_provider_id` 是后加列，多条写入路径只写了服务商**名称**
（生产取证：今日 786/832 行 ttft_ms 非空——即直连流式——但 id 为 NULL），
而聚合查询一律 `WHERE routed_provider_id IS NOT NULL` → 这些行被整行丢弃。
连带缺陷：headroom 每日限额同样统计不到，保留额度形同虚设。

修复三层：
1. 聚合层收敛到 `server/core/provider_usage.aggregate_usage_by_provider`
   （id 优先、名称兜底；排除 health_check 与 pending）
2. 写入层补齐 id（各路径 + log_queue 落库前兜底解析）
3. 历史数据启动回填 + 聚合索引
"""
import pytest
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.models.base import Base
from server.models.model import Model
from server.models.provider import Provider
from server.models.request_log import RequestLog


async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pu.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[
            Provider.__table__, Model.__table__, RequestLog.__table__,
        ])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    return engine, Session


def _log(**kw):
    kw.setdefault("status", "success")
    kw.setdefault("is_health_check", False)
    return RequestLog(**kw)


# ── 1) 核心回归：只写名称的历史行必须计入 ─────────────────

@pytest.mark.asyncio
async def test_name_only_rows_are_counted(tmp_path):
    """只写 routed_provider 名称、id 为 NULL 的行不能丢——这是用户报的原始现象。"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p1 = Provider(name="CodeBuddy CN", base_url="https://x/v1")
            p2 = Provider(name="Qoder", base_url="https://y/v1")
            db.add_all([p1, p2])
            await db.commit()
            # 直连流式历史形态：只有名称
            db.add_all([
                _log(routed_provider="CodeBuddy CN", routed_model="m1",
                     prompt_tokens=600, completion_tokens=0, ttft_ms=1200),
                _log(routed_provider="CodeBuddy CN", routed_model="m1",
                     prompt_tokens=596, completion_tokens=0, ttft_ms=900),
                _log(routed_provider="Qoder", routed_model="m2",
                     prompt_tokens=139, completion_tokens=0, ttft_ms=800),
            ])
            await db.commit()

        from server.core.provider_usage import aggregate_usage_by_provider
        async with Session() as db:
            items = await aggregate_usage_by_provider(
                db, since=datetime.utcnow() - timedelta(hours=1))
        by_name = {it["provider_name"]: it for it in items}
        # 修复前这两家会整行消失（用户看到的"显示不全"）
        assert by_name["CodeBuddy CN"]["tokens"] == 1196
        assert by_name["CodeBuddy CN"]["requests"] == 2
        assert by_name["CodeBuddy CN"]["provider_id"] == p1.id  # 按名回填到正确 id
        assert by_name["Qoder"]["tokens"] == 139
        assert by_name["Qoder"]["provider_id"] == p2.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_id_and_name_rows_merge_into_one_bucket(tmp_path):
    """同一服务商既有写 id 的新行、又有只有名称的历史行 → 合并为一行，不能出现两条。"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p = Provider(name="u1s1", base_url="https://u/v1")
            db.add(p)
            await db.commit()
            pid = p.id
            db.add_all([
                _log(routed_provider="u1s1", routed_provider_id=pid,
                     prompt_tokens=100, completion_tokens=0),
                _log(routed_provider="u1s1", routed_provider_id=None,
                     prompt_tokens=50, completion_tokens=0, ttft_ms=100),
            ])
            await db.commit()

        from server.core.provider_usage import aggregate_usage_by_provider
        async with Session() as db:
            items = await aggregate_usage_by_provider(
                db, since=datetime.utcnow() - timedelta(hours=1))
        rows = [it for it in items if it["provider_name"] == "u1s1"]
        assert len(rows) == 1, f"同服务商被拆成多行: {rows}"
        assert rows[0]["tokens"] == 150
        assert rows[0]["requests"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deleted_provider_kept_as_unknown_bucket(tmp_path):
    """服务商已删除/改名（名称匹配不到）→ 保留原始名称的独立桶，不静默丢弃数据。"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            db.add(_log(routed_provider="已删除的站", routed_provider_id=None,
                        prompt_tokens=77, completion_tokens=0))
            await db.commit()

        from server.core.provider_usage import aggregate_usage_by_provider
        async with Session() as db:
            items = await aggregate_usage_by_provider(
                db, since=datetime.utcnow() - timedelta(hours=1))
        assert len(items) == 1
        assert items[0]["provider_id"] is None
        assert items[0]["provider_name"] == "已删除的站"  # 保留名称便于排查
        assert items[0]["tokens"] == 77
    finally:
        await engine.dispose()


# ── 2) 排除规则：健康检查 / pending / 未结算 ───────────────

@pytest.mark.asyncio
async def test_health_check_rows_excluded(tmp_path):
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p = Provider(name="A", base_url="https://a/v1")
            db.add(p)
            await db.commit()
            db.add_all([
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=10, completion_tokens=0),
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=999, completion_tokens=0, is_health_check=True),
            ])
            await db.commit()

        from server.core.provider_usage import aggregate_usage_by_provider
        async with Session() as db:
            items = await aggregate_usage_by_provider(
                db, since=datetime.utcnow() - timedelta(hours=1))
        assert len(items) == 1 and items[0]["tokens"] == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_rows_excluded(tmp_path):
    """在途（pending）行服务商未定，且完成时会原位更新 → 不得提前计入用量。"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p = Provider(name="A", base_url="https://a/v1")
            db.add(p)
            await db.commit()
            db.add_all([
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=10, completion_tokens=0),
                _log(routed_provider=None, routed_provider_id=None, status="pending"),
            ])
            await db.commit()

        from server.core.provider_usage import aggregate_usage_by_provider
        async with Session() as db:
            items = await aggregate_usage_by_provider(
                db, since=datetime.utcnow() - timedelta(hours=1))
        assert len(items) == 1 and items[0]["tokens"] == 10
        assert items[0]["requests"] == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_until_bound_and_window(tmp_path):
    """时间窗（since/until）必须生效：窗口外行不计入。"""
    engine, Session = await _setup(tmp_path)
    try:
        now = datetime.utcnow()
        async with Session() as db:
            p = Provider(name="A", base_url="https://a/v1")
            db.add(p)
            await db.commit()
            db.add_all([
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=10, completion_tokens=0, created_at=now - timedelta(hours=5)),
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=20, completion_tokens=0, created_at=now - timedelta(hours=2)),
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=40, completion_tokens=0, created_at=now - timedelta(minutes=1)),
            ])
            await db.commit()

        from server.core.provider_usage import aggregate_usage_by_provider
        async with Session() as db:
            items = await aggregate_usage_by_provider(
                db, since=now - timedelta(hours=3), until=now - timedelta(minutes=30))
        assert len(items) == 1 and items[0]["tokens"] == 20  # 只有中间那条在窗内
    finally:
        await engine.dispose()


# ── 3) 聚合口径：token = prompt + completion；成本累加 ──────

@pytest.mark.asyncio
async def test_tokens_and_cost_accumulate(tmp_path):
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p = Provider(name="A", base_url="https://a/v1")
            db.add(p)
            await db.commit()
            db.add_all([
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=100, completion_tokens=50, estimated_cost_usd=0.01),
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=200, completion_tokens=25, estimated_cost_usd=0.02),
            ])
            await db.commit()

        from server.core.provider_usage import aggregate_usage_by_provider
        async with Session() as db:
            items = await aggregate_usage_by_provider(
                db, since=datetime.utcnow() - timedelta(hours=1))
        assert items[0]["tokens"] == 375
        assert items[0]["cost_usd"] == pytest.approx(0.03, abs=1e-9)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_null_token_counters_tolerated(tmp_path):
    """token 列为 NULL（媒体/失败行）不得让 SUM 变 NULL 或抛错。"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p = Provider(name="A", base_url="https://a/v1")
            db.add(p)
            await db.commit()
            db.add_all([
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=None, completion_tokens=None, media_type="image"),
                _log(routed_provider="A", routed_provider_id=p.id,
                     prompt_tokens=10, completion_tokens=None),
            ])
            await db.commit()

        from server.core.provider_usage import aggregate_usage_by_provider
        async with Session() as db:
            items = await aggregate_usage_by_provider(
                db, since=datetime.utcnow() - timedelta(hours=1))
        assert items[0]["tokens"] == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sorted_by_tokens_desc(tmp_path):
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            ps = [Provider(name=n, base_url="https://x/v1") for n in ("small", "big", "mid")]
            db.add_all(ps)
            await db.commit()
            db.add_all([
                _log(routed_provider="small", routed_provider_id=ps[0].id,
                     prompt_tokens=10, completion_tokens=0),
                _log(routed_provider="big", routed_provider_id=ps[1].id,
                     prompt_tokens=1000, completion_tokens=0),
                _log(routed_provider="mid", routed_provider_id=ps[2].id,
                     prompt_tokens=100, completion_tokens=0),
            ])
            await db.commit()

        from server.core.provider_usage import aggregate_usage_by_provider
        async with Session() as db:
            items = await aggregate_usage_by_provider(
                db, since=datetime.utcnow() - timedelta(hours=1))
        assert [it["provider_name"] for it in items] == ["big", "mid", "small"]
    finally:
        await engine.dispose()


# ── 4) headroom 接线：限额必须能统计到名称行 ───────────────

@pytest.mark.asyncio
async def test_headroom_breakdown_includes_name_only_rows(tmp_path, monkeypatch):
    """headroom 每日限额统计不到名称行 = 保留额度失效（本轮修的第二类缺陷）。"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p = Provider(name="限额站", base_url="https://x/v1")
            db.add(p)
            await db.commit()
            pid = p.id
            db.add(_log(routed_provider="限额站", routed_provider_id=None,
                        prompt_tokens=5000, completion_tokens=0, ttft_ms=300))
            await db.commit()

        from server.core import headroom_manager as hm
        monkeypatch.setattr(hm, "get_headroom_entries",
                            lambda: [{"provider_id": pid, "daily_token_limit": 4000}])
        async with Session() as db:
            breakdown = await hm.get_provider_breakdown(db)
            assert {r["provider_id"]: r["tokens"] for r in breakdown} == {pid: 5000}
            # 5000 ≥ 4000 → 该 provider 应进入 headroom 冷却（跳过路由）
            assert await hm.is_in_headroom_cooling(pid, db) is True
    finally:
        await engine.dispose()


# ── 5) 端点层：by-provider 返回完整列表 + 占比 ─────────────

@pytest.mark.asyncio
async def test_by_provider_endpoint_returns_all(tmp_path):
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p1 = Provider(name="站A", base_url="https://a/v1")
            p2 = Provider(name="站B", base_url="https://b/v1")
            db.add_all([p1, p2])
            await db.commit()
            db.add_all([
                _log(routed_provider="站A", routed_provider_id=None,  # 名称行（修复前消失）
                     prompt_tokens=75, completion_tokens=25, ttft_ms=500),
                _log(routed_provider="站B", routed_provider_id=p2.id,
                     prompt_tokens=25, completion_tokens=0),
            ])
            await db.commit()

        from server.api.admin_routing import analytics_by_provider
        async with Session() as db:
            res = await analytics_by_provider(db=db)
        names = [p["provider_name"] for p in res["providers"]]
        assert names == ["站A", "站B"]      # 两家都在，且按 token 降序
        assert res["total_tokens"] == 125
        assert res["providers"][0]["share_pct"] == 80.0
        assert res["providers"][1]["share_pct"] == 20.0
    finally:
        await engine.dispose()


# ── 6) 写入层：log_queue 落库前按名兜底解析 id ─────────────

@pytest.mark.asyncio
async def test_log_queue_backfills_provider_id(tmp_path, monkeypatch):
    """新增写入路径再漏写 id 也不会丢数据——落库前的兜底。"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p = Provider(name="兜底站", base_url="https://x/v1")
            db.add(p)
            await db.commit()
            pid = p.id

        import server.core.log_queue as lq
        monkeypatch.setattr(lq, "_get_queue", lambda: None, raising=False)
        from server.db import AsyncSessionLocal as _Real
        # _write_batch 内部 from server.db import AsyncSessionLocal，故 patch server.db 属性
        import server.db as sdb
        monkeypatch.setattr(sdb, "AsyncSessionLocal", Session)

        await lq._write_batch([{
            "conversation_id": "c-backfill",
            "requested_model": "m",
            "routed_provider": "兜底站",     # 只有名称，故意不传 id
            "routed_model": "m1",
            "status": "success",
            "prompt_tokens": 5, "completion_tokens": 5,
        }])
        async with Session() as db:
            row = (await db.execute(select(RequestLog))).scalars().one()
            assert row.routed_provider_id == pid   # 兜底解析成功
            assert row.routed_provider == "兜底站"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_log_queue_backfill_leaves_unknown_name_null(tmp_path, monkeypatch):
    """名称匹配不到（服务商已删除）时保持 NULL，不能因兜底失败丢日志。"""
    engine, Session = await _setup(tmp_path)
    try:
        import server.db as sdb
        monkeypatch.setattr(sdb, "AsyncSessionLocal", Session)
        import server.core.log_queue as lq
        await lq._write_batch([{
            "conversation_id": "c-unknown",
            "requested_model": "m",
            "routed_provider": "查无此站",
            "status": "success",
        }])
        async with Session() as db:
            row = (await db.execute(select(RequestLog))).scalars().one()
            assert row.routed_provider_id is None
            assert row.routed_provider == "查无此站"
    finally:
        await engine.dispose()


# ── 7) 迁移：历史行回填 SQL + 聚合索引 ─────────────────────

@pytest.mark.asyncio
async def test_startup_backfill_sql_repairs_history(tmp_path):
    """启动回填 SQL 必须能把历史名称行补成 id 行（幂等）。"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            p = Provider(name="历史站", base_url="https://x/v1")
            db.add(p)
            await db.commit()
            pid = p.id
            db.add(_log(routed_provider="历史站", routed_provider_id=None,
                        prompt_tokens=42, completion_tokens=0))
            await db.commit()

        _sql = ("UPDATE request_logs SET routed_provider_id = ("
                "  SELECT p.id FROM providers p WHERE p.name = request_logs.routed_provider"
                ") WHERE routed_provider_id IS NULL AND routed_provider IS NOT NULL")
        async with engine.begin() as conn:
            for _ in range(2):   # 跑两遍验证幂等
                await conn.execute(text(_sql))
        async with Session() as db:
            row = (await db.execute(select(RequestLog))).scalars().one()
            assert row.routed_provider_id == pid
    finally:
        await engine.dispose()


def test_backfill_and_index_registered_in_db_module():
    """回填语句与聚合索引必须在 db.init_db 的清单里（防止后续误删）。"""
    import inspect
    import server.db as sdb
    src = inspect.getsource(sdb.init_db)
    assert "WHERE p.name = request_logs.routed_provider" in src
    assert "idx_request_logs_prov_time" in src


# ── 8) 写入层：关键路径确实传了 id ─────────────────────────

def test_write_stream_log_accepts_provider_id():
    import inspect
    from server.api.v1_router import _write_stream_log
    sig = inspect.signature(_write_stream_log)
    assert "routed_provider_id" in sig.parameters
    src = inspect.getsource(_write_stream_log)
    # 传入 id 时必须直接用，不再按名查询（重名/改名场景会错配）
    assert "_prov_id = routed_provider_id" in src


def test_direct_stream_path_snapshots_provider_id():
    """直连流式（生产丢 id 的元凶，占当日 95%）必须快照并写入 id。"""
    import inspect
    from server.api import v1_router
    src = inspect.getsource(v1_router._chat_completions_impl)
    assert "_rt_prov_id = getattr(route_result.provider, \"id\", None)" in src
    assert "routed_provider_id=_rt_prov_id," in src


def test_media_and_passthrough_paths_pass_provider_id():
    """媒体生成与透传路径同样要落 id。"""
    import inspect
    from server.api import media_router, passthrough_router
    assert "provider_id: int = None" in inspect.getsource(media_router._write_media_log)
    assert inspect.getsource(media_router._write_media_log).count("routed_provider_id=provider_id") == 1
    psrc = inspect.getsource(passthrough_router)
    assert psrc.count("routed_provider_id=provider.id") == 2


def test_playground_and_health_checker_pass_provider_id():
    import inspect
    from server.api import admin_router
    from server.core import health_checker
    assert "routed_provider_id=getattr(_route_result.provider" in inspect.getsource(admin_router)
    assert "routed_provider_id=getattr(provider, \"id\", None)" in inspect.getsource(health_checker)


def test_frontend_detail_modal_matches_by_name_fallback():
    from pathlib import Path
    src = Path("client/src/views/Providers.vue").read_text(encoding="utf-8")
    assert "x.provider_name === p.name" in src


def test_frontend_analytics_shows_provider_count():
    from pathlib import Path
    src = Path("client/src/views/Analytics.vue").read_text(encoding="utf-8")
    assert "providerData.length }} 家" in src
    assert "（已删除）" in src
