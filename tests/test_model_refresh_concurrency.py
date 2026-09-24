"""模型刷新并发化 + 硬超时 测试（2026-09）。

背景（用户反馈）：刷新模型太慢——57 个服务商串行，实测平均 17s/个、最慢 141s，
一次全量要十几分钟。改造为：并发 + 单服务商硬超时（超时判失败不等待）+ 设置页可配。

被测对象：`admin_router.refresh_providers_concurrent`（端点调用它，故测试覆盖真实逻辑）。
"""
import asyncio
import time

import pytest


def _mk_providers(n):
    class _P:
        def __init__(self, i):
            self.id = i
            self.name = f"p{i}"
    return [_P(i) for i in range(1, n + 1)]


def _patch_session(monkeypatch, providers):
    """替换 AsyncSessionLocal：让 get(Provider, pid) 返回对应服务商。"""
    from server.api import admin_router as ar

    by_id = {p.id: p for p in providers}

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, pid):
            return by_id.get(pid)

    class _Factory:
        def __call__(self):
            return _S()

    monkeypatch.setattr(ar, "AsyncSessionLocal", _Factory())


class TestConfigDefaults:
    def test_new_fields_present(self):
        from server.config import ModelRefreshConfig
        c = ModelRefreshConfig()
        assert c.concurrency == 12
        assert c.provider_timeout_seconds == 45
        assert c.timeout_seconds == 20          # 旧字段不变

    def test_readable_via_get_config(self):
        from server.config import get_config
        mr = get_config().model_refresh
        for f in ("concurrency", "provider_timeout_seconds", "timeout_seconds"):
            assert hasattr(mr, f), f"缺少 {f}"


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_runs_concurrently(self, monkeypatch):
        """3 个各睡 0.3s 的服务商，并发 3 应 ~0.3s 而非 ~0.9s"""
        from server.api import admin_router as ar
        providers = _mk_providers(3)
        _patch_session(monkeypatch, providers)
        seen = {"active": 0, "peak": 0}

        class _Cat:
            async def refresh_models_from_provider(self, s, p, km, trigger="manual"):
                seen["active"] += 1
                seen["peak"] = max(seen["peak"], seen["active"])
                await asyncio.sleep(0.3)
                seen["active"] -= 1
                return {"added": 1, "updated": 0, "total": 1, "removed": 0}

        t0 = time.monotonic()
        results, errors = await ar.refresh_providers_concurrent(
            providers, trigger="manual", concurrency=3, provider_timeout=30,
            catalog=_Cat(), key_manager=None)
        elapsed = time.monotonic() - t0
        assert seen["peak"] >= 2, f"未并发（峰值 {seen['peak']}）"
        assert elapsed < 0.8, f"并发应快于串行 0.9s，实测 {elapsed:.2f}s"
        assert len(results) == 3 and not errors

    @pytest.mark.asyncio
    async def test_concurrency_cap_respected(self, monkeypatch):
        """并发上限真的生效（5 个任务、限 2）"""
        from server.api import admin_router as ar
        providers = _mk_providers(5)
        _patch_session(monkeypatch, providers)
        seen = {"active": 0, "peak": 0}

        class _Cat:
            async def refresh_models_from_provider(self, s, p, km, trigger="manual"):
                seen["active"] += 1
                seen["peak"] = max(seen["peak"], seen["active"])
                await asyncio.sleep(0.15)
                seen["active"] -= 1
                return {"added": 0}

        await ar.refresh_providers_concurrent(
            providers, trigger="manual", concurrency=2, provider_timeout=30,
            catalog=_Cat())
        assert seen["peak"] <= 2, f"并发超限（峰值 {seen['peak']}）"

    @pytest.mark.asyncio
    async def test_single_provider_serial(self, monkeypatch):
        from server.api import admin_router as ar
        providers = _mk_providers(1)
        _patch_session(monkeypatch, providers)
        seen = {"peak": 0, "active": 0}

        class _Cat:
            async def refresh_models_from_provider(self, s, p, km, trigger="manual"):
                seen["active"] += 1
                seen["peak"] = max(seen["peak"], seen["active"])
                await asyncio.sleep(0.05)
                seen["active"] -= 1
                return {"added": 1}

        await ar.refresh_providers_concurrent(
            providers, trigger="manual", concurrency=8, provider_timeout=30,
            catalog=_Cat())
        assert seen["peak"] == 1


class TestHardTimeout:
    @pytest.mark.asyncio
    async def test_timeout_marks_failed_without_waiting(self, monkeypatch):
        """卡死 10s 的服务商在 5s 上限后判失败，不拖累整体"""
        from server.api import admin_router as ar
        providers = _mk_providers(3)
        _patch_session(monkeypatch, providers)
        # 避免真写日志
        async def _noop_log(*a, **k):
            return None
        monkeypatch.setattr(ar, "_log_refresh_timeout", _noop_log)

        class _Cat:
            async def refresh_models_from_provider(self, s, p, km, trigger="manual"):
                if p.id == 2:
                    await asyncio.sleep(10)     # 模拟卡死（如 b.ai 实测 141s）
                return {"added": 1, "updated": 0, "total": 1, "removed": 0}

        t0 = time.monotonic()
        results, errors = await ar.refresh_providers_concurrent(
            providers, trigger="manual", concurrency=3, provider_timeout=5,
            catalog=_Cat())
        elapsed = time.monotonic() - t0
        assert elapsed < 8, f"超时未生效，等了 {elapsed:.1f}s"
        assert 2 in errors and "超时" in errors[2]
        assert errors[2].startswith("超时（>5s）")
        # 其余两个正常完成
        assert set(results) == {1, 3}
        assert sum(r.get("added", 0) for r in results.values()) == 2

    @pytest.mark.asyncio
    async def test_exception_isolated(self, monkeypatch):
        """一个服务商抛异常不中断其它"""
        from server.api import admin_router as ar
        providers = _mk_providers(3)
        _patch_session(monkeypatch, providers)

        async def _noop_log(*a, **k):
            return None
        monkeypatch.setattr(ar, "_log_refresh_timeout", _noop_log)

        class _Cat:
            async def refresh_models_from_provider(self, s, p, km, trigger="manual"):
                if p.id == 1:
                    raise RuntimeError("boom")
                return {"added": 2, "updated": 0, "total": 2, "removed": 0}

        results, errors = await ar.refresh_providers_concurrent(
            providers, trigger="manual", concurrency=3, provider_timeout=30,
            catalog=_Cat())
        assert sum(r.get("added", 0) for r in results.values()) == 4
        assert 1 in errors and "boom" in errors[1]

    @pytest.mark.asyncio
    async def test_deleted_provider_skipped(self, monkeypatch):
        """刷新途中被删的服务商：记错误但不崩"""
        from server.api import admin_router as ar
        providers = _mk_providers(2)
        _patch_session(monkeypatch, providers)

        class _Cat:
            async def refresh_models_from_provider(self, s, p, km, trigger="manual"):
                return {"added": 1}

        # 让 id=2 查不到
        orig_get = None
        from server.api import admin_router as _ar

        class _S:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, model, pid):
                return providers[0] if pid == 1 else None

        monkeypatch.setattr(_ar, "AsyncSessionLocal", lambda: _S())
        results, errors = await ar.refresh_providers_concurrent(
            providers, trigger="manual", concurrency=2, provider_timeout=10,
            catalog=_Cat())
        assert 1 in results
        assert 2 in errors and "已删除" in errors[2]


class TestEndpointSourceGuards:
    def test_endpoint_delegates_to_helper(self):
        """端点必须调用抽出的并发函数（防有人改回串行循环）"""
        import inspect
        from server.api import admin_router
        src = inspect.getsource(admin_router.refresh_models)
        assert "refresh_providers_concurrent" in src
        # 不应再出现串行 for 循环直调
        assert "for provider in providers:\n        result = await _model_catalog" not in src

    def test_helper_uses_wait_for_and_semaphore(self):
        import inspect
        from server.api import admin_router
        src = inspect.getsource(admin_router.refresh_providers_concurrent)
        assert "asyncio.wait_for" in src
        assert "Semaphore" in src
        assert "_sf()" in src, "必须每个服务商独立 session（走可注入工厂）"

    def test_factory_defaults_to_global(self):
        """未注入时用全局 AsyncSessionLocal（生产路径）"""
        import inspect
        from server.api import admin_router
        src = inspect.getsource(admin_router.refresh_providers_concurrent)
        assert "AsyncSessionLocal" in src

    def test_timeout_logged_to_refresh_logs(self):
        import inspect
        from server.api import admin_router
        src = inspect.getsource(admin_router._log_refresh_timeout)
        assert "ModelRefreshLog" in src and "ok=False" in src


class TestOpsEndpoints:
    def test_get_put_registered(self):
        from server.api import admin_ops_router as ops
        paths = {r.path for r in ops.router.routes}
        # router 有 prefix（/admin/api），故按后缀断言
        assert any(p.endswith("/model-refresh") for p in paths), paths

    def test_put_clamps_values(self):
        import inspect
        from server.api import admin_ops_router as ops
        src = inspect.getsource(ops.put_model_refresh_config)
        assert "max(1, min(32" in src        # concurrency
        assert "max(5, min(1800" in src      # provider_timeout_seconds
        assert "max(3, min(600" in src       # timeout_seconds

    def test_get_returns_all_fields(self):
        from server.config import ModelRefreshConfig
        d = ModelRefreshConfig().model_dump()
        for f in ("concurrency", "provider_timeout_seconds", "timeout_seconds",
                  "scheduled_enabled", "interval_minutes", "remove_missing_models"):
            assert f in d


class TestSessionFactoryDerivation:
    """并发任务必须落在**调用方所在的那个库**（生产=全局库，测试=临时库）。"""

    @pytest.mark.asyncio
    async def test_derives_from_caller_session(self, tmp_path):
        """临时库 session → 派生的工厂也应连临时库（否则并发写错库）"""
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from server.api.admin_router import _session_factory_for
        from server.models.base import Base
        from server.models.provider import Provider
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        try:
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.create_all, tables=[Provider.__table__])
            SF = async_sessionmaker(eng, expire_on_commit=False)
            async with SF() as db:
                derived = _session_factory_for(db)
            # 派生的 session 能查到临时库的表（若错用全局库会报表不存在）
            async with derived() as s2:
                from sqlalchemy import select
                await s2.execute(select(Provider).limit(1))
        finally:
            await eng.dispose()

    @pytest.mark.asyncio
    async def test_falls_back_safely_on_weird_session(self):
        """异常对象不能让派生崩掉（必须安全回退）"""
        from server.api.admin_router import _session_factory_for, AsyncSessionLocal
        class _Weird:
            @property
            def bind(self):
                raise RuntimeError("nope")
        assert _session_factory_for(_Weird()) is AsyncSessionLocal


class TestResponseSchema:
    def test_new_fields(self):
        from server.schemas.provider import ModelsRefreshResponse
        r = ModelsRefreshResponse(added=0, updated=0, total=0)
        assert r.failed_details == []
        assert r.duration_ms == 0
