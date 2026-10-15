"""过载保护回归测试（2026-10-15 生产事故配套）。

覆盖三件套：
1. server/core/inflight_gate.py —— 推理入口全局并发闸（超出快速失败 503）
2. server/core/exception_guard.py —— 兜底异常护栏 + 错误日志防洪
3. v1_router._release_db —— DB 连接不横跨上游调用（连接池治理）
"""
import asyncio
import json

import pytest


# ── 并发闸：路径判定 ──────────────────────────────────────────

def test_gate_paths_matched():
    from server.core.inflight_gate import _gated
    # 生成类入口全部命中
    assert _gated("/v1/chat/completions")
    assert _gated("/v1/messages")
    assert _gated("/v1/responses")
    assert _gated("/v1beta/models/gemini-3.1-pro:generateContent")
    assert _gated("/v1beta/models/gemini-3.1-pro:streamGenerateContent")
    # combo 前缀形态（剥前缀后是生成类）
    assert _gated("/combo:918/v1/chat/completions")
    assert _gated("/combo:my-fast/v1/messages")


def test_gate_paths_not_matched():
    from server.core.inflight_gate import _gated
    assert not _gated("/v1/models")
    assert not _gated("/health")
    assert not _gated("/admin/api/providers")
    assert not _gated("/v1beta/models")           # 列表，不是生成
    assert not _gated("/combo:918/v1/models")     # 组合模型列表
    assert not _gated("/v1/embeddings")


# ── 并发闸：过载快速失败 ──────────────────────────────────────

def _build_gate_app(max_inflight=1, timeout=0.2):
    """构造只含闸的极简 ASGI 应用；handler 阻塞直到 release 事件。"""
    from fastapi import FastAPI, Request
    from server.core.inflight_gate import InflightGateMiddleware

    app = FastAPI()
    app.add_middleware(InflightGateMiddleware)

    release = asyncio.Event()
    entered = asyncio.Event()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        entered.set()
        await release.wait()
        return {"ok": True}

    @app.get("/v1/models")
    async def models():
        return {"models": []}

    # 让闸读到我们想要的配置（monkeypatch 配置单例太重，直接改中间件读的 get_config）
    _max, _to = max_inflight, timeout

    class _C:
        enabled = True
        max_inflight = _max
        acquire_timeout_seconds = _to

    class _Cfg:
        concurrency = _C()

    import server.config as _cfg_mod
    orig = _cfg_mod.get_config
    _cfg_mod.get_config = lambda: _Cfg()
    app._restore_config = lambda: setattr(_cfg_mod, "get_config", orig)
    return app, release, entered


def test_gate_rejects_when_full():
    import httpx

    async def _t():
        app, release, entered = _build_gate_app(max_inflight=1, timeout=0.2)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as c:
                # 第一个请求占住唯一名额（在 handler 里阻塞）
                t1 = asyncio.create_task(c.post("/v1/chat/completions", json={}))
                await asyncio.wait_for(entered.wait(), 2)
                # 第二个请求：闸满 → 等 0.2s 后快速失败 503
                r2 = await c.post("/v1/chat/completions", json={})
                assert r2.status_code == 503
                body = r2.json()
                assert body["error"]["code"] == "gateway_overloaded"
                assert r2.headers.get("retry-after") == "2"
                # 放行第一个
                release.set()
                r1 = await asyncio.wait_for(t1, 3)
                assert r1.status_code == 200
        finally:
            app._restore_config()

    asyncio.run(_t())


def test_gate_passes_non_gated_paths_when_full():
    import httpx

    async def _t():
        app, release, entered = _build_gate_app(max_inflight=1, timeout=0.2)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as c:
                t1 = asyncio.create_task(c.post("/v1/chat/completions", json={}))
                await asyncio.wait_for(entered.wait(), 2)
                # 非生成类路径不受闸影响，闸满也直接通过
                r = await c.get("/v1/models")
                assert r.status_code == 200
                release.set()
                await asyncio.wait_for(t1, 3)
        finally:
            app._restore_config()

    asyncio.run(_t())


def test_gate_disabled_passes_everything():
    import httpx

    async def _t():
        from fastapi import FastAPI
        from server.core.inflight_gate import InflightGateMiddleware

        app = FastAPI()
        app.add_middleware(InflightGateMiddleware)

        @app.post("/v1/chat/completions")
        async def chat():
            return {"ok": True}

        class _C:
            enabled = False
            max_inflight = 1
            acquire_timeout_seconds = 0.1

        class _Cfg:
            concurrency = _C()

        import server.config as _cfg_mod
        orig = _cfg_mod.get_config
        _cfg_mod.get_config = lambda: _Cfg()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as c:
                # 闸关：两个并发请求都应通过（第二个不会被 503）
                rs = await asyncio.gather(c.post("/v1/chat/completions", json={}),
                                          c.post("/v1/chat/completions", json={}))
                assert [r.status_code for r in rs] == [200, 200]
        finally:
            _cfg_mod.get_config = orig

    asyncio.run(_t())


# ── 异常护栏：分类与防洪 ──────────────────────────────────────

def test_guard_classifies_pool_timeout_as_503():
    from server.core.exception_guard import _classify
    import sqlalchemy.exc as se
    status, code, _ = _classify(se.TimeoutError("QueuePool limit of size 20 overflow 10 reached"))
    assert status == 503 and code == "database_pool_timeout"
    status, code, _ = _classify(asyncio.TimeoutError())
    assert status == 503 and code == "timeout"
    status, code, _ = _classify(ValueError("boom"))
    assert status == 500 and code == "internal_error"


def test_guard_flood_limiting():
    from server.core import exception_guard as eg
    eg._flood.clear()
    bucket = "guard-flood-test"
    # 窗口内第一条记录，其余被压制
    assert eg._log_limited(bucket, "first") == 0
    for _ in range(5):
        assert eg._log_limited(bucket, "more") == 1
    start, suppressed = eg._flood[bucket]
    assert suppressed == 5
    # 模拟窗口过期：把窗口起点拨回过去
    import time as _t
    eg._flood[bucket] = (_t.monotonic() - eg._FLOOD_WINDOW - 1, suppressed)
    assert eg._log_limited(bucket, "next window") == 0


def test_guard_returns_503_json_for_unhandled_pool_error():
    """端到端：未处理的 QueuePool 类异常 → 503 短 JSON（不再冒泡出 180 行栈）。"""
    import httpx
    import sqlalchemy.exc as se

    async def _t():
        from fastapi import FastAPI
        from server.core.exception_guard import ExceptionGuardMiddleware

        app = FastAPI()
        app.add_middleware(ExceptionGuardMiddleware)

        @app.get("/boom")
        async def boom():
            raise se.TimeoutError("QueuePool limit of size 20 overflow 10 reached, connection timed out")

        # 护栏注册为最外层用户中间件 → 先于 ServerErrorMiddleware 看到异常，
        # 未处理的 QueuePool 类异常必须变成 503 短 JSON（而不是 500 长栈）。
        from starlette.testclient import TestClient
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/boom")
            assert r.status_code == 503
            assert r.json()["error"]["code"] == "database_pool_timeout"
            assert "QueuePool" not in r.text  # 内部细节不泄漏给客户端

    asyncio.run(_t())


# ── 连接释放：_release_db 后会话可复用 ─────────────────────────

def test_release_db_frees_pool_connection(tmp_path):
    """核心回归：读查询签出连接 → _release_db → 另一会话可取到连接（池未独占）。"""
    async def _t():
        from sqlalchemy import select, text
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

        class Base(DeclarativeBase):
            pass

        class T(Base):
            __tablename__ = "t"
            id: Mapped[int] = mapped_column(primary_key=True)
            name: Mapped[str] = mapped_column()

        db_path = tmp_path / "t.db"
        eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}",
                                  pool_size=1, max_overflow=0, pool_timeout=0.5)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        async with S() as s0:
            s0.add(T(id=1, name="x"))
            await s0.commit()

        from server.api.v1_router import _release_db

        s1 = S()
        row = (await s1.execute(select(T))).scalar_one()
        assert row.name == "x"
        # 不释放时：池（size=1）被 s1 独占 → s2 必超时
        s2 = S()
        with pytest.raises(Exception):
            await s2.execute(select(T))
        # 释放后：s2 拿到连接；s1 属性仍可读
        await _release_db(s1)
        row2 = (await s2.execute(select(T))).scalar_one()
        assert row2.name == "x"
        assert row.name == "x"                      # close 后已加载属性仍可读
        # s2 也释放后 s1 复用可行（重新签出连接）——池 size=1，需互相让路
        await _release_db(s2)
        row3 = (await s1.execute(select(T))).scalar_one()
        assert row3.name == "x"
        await s1.close()
        await s2.close()
        await eng.dispose()

    asyncio.run(_t())


def test_release_db_never_raises():
    """幂等/容错：任意假对象都不抛异常。"""
    async def _t():
        from server.api.v1_router import _release_db
        class _Broken:
            async def close(self):
                raise RuntimeError("nope")
        await _release_db(_Broken())     # 不抛
        await _release_db(None)          # 不抛（close 属性缺失被兜住）
    asyncio.run(_t())


# ── 配置：ConcurrencyConfig 默认值与注册 ──────────────────────

def test_concurrency_config_defaults_and_registration():
    from server.config import Config, ConcurrencyConfig
    c = ConcurrencyConfig()
    assert c.enabled is True
    assert c.max_inflight == 128
    assert c.acquire_timeout_seconds == 15.0
    # 挂在顶层 Config 上（config.yaml 缺省段时也要能取到默认值）
    assert Config().concurrency.max_inflight == 128


def test_concurrency_config_loads_from_yaml_dict():
    from server.config import Config
    cfg = Config(**{"concurrency": {"max_inflight": 64, "acquire_timeout_seconds": 5}})
    assert cfg.concurrency.max_inflight == 64
    assert cfg.concurrency.acquire_timeout_seconds == 5
    assert cfg.concurrency.enabled is True
