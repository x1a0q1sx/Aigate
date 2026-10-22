# -*- coding: utf-8 -*-
"""分析端点时间范围扩展（2026-09-30 仪表盘/分析页重设计）。

覆盖：
- trend：bucket=hour/day/week/month 分桶 + start/end 窗口 + 空桶补零（仅 day/hour）
  + 健康检查行排除
- today：窗口内 avg_latency_ms / avg_ttft_ms / ttft_samples；无样本为 None；date 形式 end 含当日 23:59:59
- by-provider：start/end 过滤（含只写名称的历史行）

锚点：2026-09-28 是周一。所有断言显式传 start/end（不依赖真实 utcnow 的默认窗口）。
"""
import pytest
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api import admin_routing
from server.models.base import Base
from server.models.model import Model
from server.models.provider import Provider
from server.models.request_log import RequestLog


async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/ar.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[
            Provider.__table__, Model.__table__, RequestLog.__table__,
        ])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    return engine, Session


def _log(**kw):
    kw.setdefault("status", "success")
    kw.setdefault("is_health_check", False)
    kw.setdefault("prompt_tokens", 100)
    kw.setdefault("completion_tokens", 50)
    kw.setdefault("estimated_cost_usd", 0.001)
    kw.setdefault("latency_ms", 1000)
    return RequestLog(**kw)


# ── trend ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trend_day_zero_fill_and_health_check_excluded(tmp_path):
    """day 桶：空桶补零；健康检查行不计入（与 today/by-provider 口径一致）"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            db.add(_log(created_at=datetime(2026, 9, 28, 10, 0)))
            db.add(_log(created_at=datetime(2026, 9, 28, 11, 0)))
            db.add(_log(created_at=datetime(2026, 9, 28, 12, 0), is_health_check=True))
            db.add(_log(created_at=datetime(2026, 9, 30, 10, 0)))
            await db.commit()
            res = await admin_routing.analytics_trend(
                days=9, bucket="day", start="2026-09-22", end="2026-09-30", db=db)
        assert len(res) == 9
        by_day = {r["day"]: r for r in res}
        assert by_day["2026-09-28"]["requests"] == 2  # 健康检查行被排除
        assert by_day["2026-09-28"]["tokens"] == 300
        assert by_day["2026-09-30"]["requests"] == 1
        assert by_day["2026-09-29"]["requests"] == 0  # 空桶补零
        assert by_day["2026-09-22"]["requests"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_trend_hour_bucket(tmp_path):
    """hour 桶：key 格式 YYYY-MM-DDTHH:00:00，按小时归并 + 补零"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            db.add(_log(created_at=datetime(2026, 9, 28, 8, 15)))
            db.add(_log(created_at=datetime(2026, 9, 28, 8, 45)))
            db.add(_log(created_at=datetime(2026, 9, 28, 10, 5)))
            await db.commit()
            res = await admin_routing.analytics_trend(
                days=1, bucket="hour", start="2026-09-28T00:00", end="2026-09-28T23:59", db=db)
        assert len(res) == 24
        by_h = {r["day"]: r for r in res}
        assert by_h["2026-09-28T08:00:00"]["requests"] == 2
        assert by_h["2026-09-28T10:00:00"]["requests"] == 1
        assert by_h["2026-09-28T00:00:00"]["requests"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_trend_week_month_merge(tmp_path):
    """week/month 桶：天级分组在 Python 侧归并（周一为周槽、YYYY-MM 为月槽，不补零）"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            # 2026-09-28 周一；09-30 周三（同一周）；09-15 周二（属周一=09-14 的周）
            db.add(_log(created_at=datetime(2026, 9, 28, 10, 0), prompt_tokens=10, completion_tokens=0))
            db.add(_log(created_at=datetime(2026, 9, 30, 10, 0), prompt_tokens=20, completion_tokens=0))
            db.add(_log(created_at=datetime(2026, 9, 15, 10, 0), prompt_tokens=40, completion_tokens=0))
            await db.commit()
            res = await admin_routing.analytics_trend(
                days=30, bucket="week", start="2026-09-14", end="2026-10-04", db=db)
        by_w = {r["day"]: r for r in res}
        assert by_w["2026-09-28"]["requests"] == 2
        assert by_w["2026-09-28"]["tokens"] == 30
        assert by_w["2026-09-14"]["requests"] == 1
        assert by_w["2026-09-14"]["tokens"] == 40
        engine2, Session2 = None, None
        # month 桶（同一套数据）
        async with Session() as db:
            res2 = await admin_routing.analytics_trend(
                days=90, bucket="month", start="2026-08-01", end="2026-09-30", db=db)
        # 8 月无数据 → week/month 稀疏桶不补零，只剩观测桶
        assert [r["day"] for r in res2] == ["2026-09"]
        assert res2[0]["requests"] == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_trend_start_end_window(tmp_path):
    """start/end 提供后精确生效（days 被覆盖）"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            db.add(_log(created_at=datetime(2026, 9, 27, 23, 0)))
            db.add(_log(created_at=datetime(2026, 9, 28, 1, 0)))
            await db.commit()
            res = await admin_routing.analytics_trend(
                days=7, bucket="hour", start="2026-09-28T00:00", end="2026-09-28T06:00", db=db)
        assert len(res) == 7
        assert sum(r["requests"] for r in res) == 1
    finally:
        await engine.dispose()


# ── today ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_today_latency_and_range(tmp_path):
    """avg_latency_ms / avg_ttft_ms / ttft_samples；窗口外与健康检查行不计；空窗为 None"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            db.add(_log(created_at=datetime(2026, 9, 28, 8, 0), latency_ms=1000, ttft_ms=200))
            db.add(_log(created_at=datetime(2026, 9, 28, 9, 0), latency_ms=3000, ttft_ms=600, status="error"))
            db.add(_log(created_at=datetime(2026, 9, 27, 9, 0), latency_ms=99999))  # 窗口外
            db.add(_log(created_at=datetime(2026, 9, 28, 10, 0), is_health_check=True, latency_ms=1))
            await db.commit()
            res = await admin_routing.analytics_today(start="2026-09-28", end="2026-09-28", db=db)
        assert res["requests"] == 2
        assert res["success_requests"] == 1
        assert res["avg_latency_ms"] == 2000.0
        assert res["avg_ttft_ms"] == 400.0
        assert res["ttft_samples"] == 2
        async with Session() as db:
            res2 = await admin_routing.analytics_today(start="2026-09-30", end="2026-09-30", db=db)
        assert res2["requests"] == 0
        assert res2["avg_latency_ms"] is None
        assert res2["avg_ttft_ms"] is None
        assert res2["ttft_samples"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_today_end_of_day_inclusive(tmp_path):
    """end 传日期形式 → 含当日 23:59:59"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            db.add(_log(created_at=datetime(2026, 9, 28, 23, 30)))
            await db.commit()
            res = await admin_routing.analytics_today(start="2026-09-28", end="2026-09-28", db=db)
        assert res["requests"] == 1
    finally:
        await engine.dispose()


# ── by-provider ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_by_provider_start_end(tmp_path):
    """start/end 过滤：窗口外行不计；只写名称的历史行照常聚合"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            db.add(_log(created_at=datetime(2026, 9, 28, 8, 0),
                        routed_provider="Alpha", prompt_tokens=100, completion_tokens=50))
            db.add(_log(created_at=datetime(2026, 9, 29, 8, 0),
                        routed_provider="Alpha", prompt_tokens=10, completion_tokens=0))
            db.add(_log(created_at=datetime(2026, 9, 26, 8, 0),
                        routed_provider="Alpha", prompt_tokens=999, completion_tokens=0))  # 窗口外
            await db.commit()
            res = await admin_routing.analytics_by_provider(start="2026-09-27", end="2026-09-30", db=db)
        alpha = next(p for p in res["providers"] if p["provider_name"] == "Alpha")
        assert alpha["requests"] == 2
        assert alpha["tokens"] == 160
    finally:
        await engine.dispose()


# ── by-provider: 三视角（2026-10-20） ─────────────────────

@pytest.mark.asyncio
async def test_by_provider_dimension_model_and_account(tmp_path):
    """三种视角：服务商 / 服务商×模型 / 服务商×账号（模型、账号各自成行）"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            # 同一服务商下 2 个模型、2 个账号，另有 1 条无账号（非 OAuth 路径）
            db.add(_log(created_at=datetime(2026, 9, 28, 8, 0), routed_provider="Alpha",
                        routed_model="m1", routed_account="a@x.com",
                        prompt_tokens=100, completion_tokens=0))
            db.add(_log(created_at=datetime(2026, 9, 28, 9, 0), routed_provider="Alpha",
                        routed_model="m1", routed_account="b@x.com",
                        prompt_tokens=50, completion_tokens=0))
            db.add(_log(created_at=datetime(2026, 9, 28, 10, 0), routed_provider="Alpha",
                        routed_model="m2", routed_account="a@x.com",
                        prompt_tokens=10, completion_tokens=0))
            db.add(_log(created_at=datetime(2026, 9, 28, 11, 0), routed_provider="Beta",
                        routed_model="m3", prompt_tokens=7, completion_tokens=0))
            await db.commit()

            base = dict(start="2026-09-28", end="2026-09-28", db=db)
            by_prov = await admin_routing.analytics_by_provider(dimension="provider", **base)
            by_model = await admin_routing.analytics_by_provider(dimension="provider_model", **base)
            by_acct = await admin_routing.analytics_by_provider(dimension="provider_account", **base)

        # 服务商视角：只有 2 行，Alpha 汇总 160 token
        assert {p["provider_name"] for p in by_prov["providers"]} == {"Alpha", "Beta"}
        alpha = next(p for p in by_prov["providers"] if p["provider_name"] == "Alpha")
        assert alpha["tokens"] == 160 and alpha["requests"] == 3
        assert by_prov["dimension"] == "provider"

        # 模型视角：Alpha×m1 合并两条（150），Alpha×m2 一条，Beta×m3 一条
        rows = {(r["provider_name"], r["model"]): r for r in by_model["providers"]}
        assert len(rows) == 3
        assert rows[("Alpha", "m1")]["tokens"] == 150
        assert rows[("Alpha", "m1")]["requests"] == 2
        assert rows[("Alpha", "m2")]["tokens"] == 10
        assert rows[("Beta", "m3")]["tokens"] == 7
        assert sum(r["share_pct"] for r in by_model["providers"]) == pytest.approx(100, abs=0.5)

        # 账号视角：Alpha×a@x.com 合并两条（110），b@x.com 一条；Beta 无账号 → account=None
        acct = {(r["provider_name"], r["account"]): r for r in by_acct["providers"]}
        assert acct[("Alpha", "a@x.com")]["tokens"] == 110
        assert acct[("Alpha", "b@x.com")]["tokens"] == 50
        assert acct[("Beta", None)]["tokens"] == 7
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_by_provider_unknown_dimension_falls_back(tmp_path):
    """未知视角不应报错/注入 SQL，退回服务商视角（headroom 等旧调用零影响）"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            db.add(_log(created_at=datetime(2026, 9, 28, 8, 0), routed_provider="Alpha",
                        routed_model="m1", prompt_tokens=5, completion_tokens=0))
            await db.commit()
            res = await admin_routing.analytics_by_provider(
                start="2026-09-28", end="2026-09-28", dimension="bogus", db=db)
        # 直接调函数时 Query 约束不生效，聚合层按白名单退回 provider
        assert res["providers"][0]["provider_name"] == "Alpha"
        assert "model" not in res["providers"][0]
    finally:
        await engine.dispose()
