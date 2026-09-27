# -*- coding: utf-8 -*-
"""自定义头值必须恒为 str —— 2026-09-27 生产事故的回归锁。

事故链（生产日志实证）：
    /providers/import 不做类型校验，把 {"x-video-timeout": 1800}（int）写进库
      → ① `ProviderResponse.headers: Dict[str, str]` 校验失败
           → GET /admin/api/providers 整体 500（生产 13 次）
           → 服务商管理页空白；模型页 Promise.all 被同一 500 带崩
      → ② httpx 拒绝非 str 头值（TypeError: Header value must be str or bytes）
           → 该服务商**所有推理请求**失败（aistudio 请求日志实测到）

四层防御各有一组测试：
    1) ORM HeaderJSON（写入/读取双向归一，含绕过 ORM 写入的历史脏数据）
    2) schema 校验器（写入侧严 → 422；读取侧宽 → 绝不 500）
    3) 启动存量修复（幂等、不动 updated_at、非 dict 值丢弃）
    4) 出站净化（内部 __ 键剥离 + 值归一，httpx 真正能收）
"""
import asyncio
import json

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.header_values import normalize_map, outbound_headers
from server.models.provider import HeaderJSON, Provider


# ── 1) 归一化函数本体 ──────────────────────────────────

def test_normalize_scalars_to_str():
    assert normalize_map({"a": 1800}) == {"a": "1800"}
    assert normalize_map({"a": 1.5}) == {"a": "1.5"}
    assert normalize_map({"a": True}) == {"a": "true"}
    assert normalize_map({"a": False}) == {"a": "false"}
    assert normalize_map({"a": "v"}) == {"a": "v"}


def test_normalize_none_and_non_dict_passthrough():
    # None 表示"未提供"，不能被吞成 {}
    assert normalize_map(None) is None
    assert normalize_map("notadict") == "notadict"


def test_normalize_lenient_drops_non_scalars():
    """读取/出站侧：dict/list 值丢弃，绝不抛异常（事故形态是接口 500）。"""
    assert normalize_map({"ok": "v", "bad": {"n": 1}, "bad2": [1]}) == {"ok": "v"}


def test_normalize_strict_raises_on_non_scalars():
    """写入侧：dict/list 值必须报错，垃圾不进库。"""
    with pytest.raises(ValueError):
        normalize_map({"bad": {"n": 1}}, strict=True)
    with pytest.raises(ValueError):
        normalize_map({"bad": [1]}, strict=True)


def test_normalize_int_key_becomes_str():
    assert normalize_map({1: "v"}) == {"1": "v"}


# ── 2) 出站净化 ────────────────────────────────────────

def test_outbound_strips_all_internal_keys():
    """__ 前缀 = 网关内部键，一律不出站（此前各适配器各维护一份名单，漏一个就是一类故障）。"""
    got = outbound_headers({
        "__oauth": True, "__proxy_force": True, "__proxy_url": "http://p",
        "__fg": "0", "__dpop": {"jwk": 1}, "__baseUrl": "http://x", "__refresh": True,
        "X-Real": "v",
    })
    assert got == {"X-Real": "v"}


def test_outbound_normalizes_values():
    assert outbound_headers({"x-video-timeout": 1800, "b": False}) == {
        "x-video-timeout": "1800", "b": "false"}


def test_outbound_non_dict_is_empty():
    assert outbound_headers(None) == {}
    assert outbound_headers("nope") == {}


def test_outbound_result_is_httpx_safe():
    """核心断言：净化后的头 httpx 必须能收（事故里正是这一步抛 TypeError）。"""
    headers = outbound_headers({"__oauth": True, "x-video-timeout": 1800})
    req = httpx.Request("POST", "https://x.invalid/v1/chat/completions", headers=headers)
    assert req.headers["x-video-timeout"] == "1800"


def test_raw_int_header_would_break_httpx():
    """反证：未净化的 int 头确实会被 httpx 拒绝 —— 证明净化不是多余步骤。"""
    with pytest.raises(TypeError):
        httpx.Request("POST", "https://x.invalid/v1/chat/completions",
                      headers={"x-video-timeout": 1800})


# ── 3) ORM 层 HeaderJSON ───────────────────────────────

def _session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/h.db")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_orm_write_normalizes_on_refresh(tmp_path):
    engine, Session = _session(tmp_path)
    try:
        async with engine.begin() as c:
            await c.run_sync(Provider.metadata.create_all, tables=[Provider.__table__])
        async with Session() as s:
            p = Provider(name="t", base_url="http://x", api_type="openai_compat",
                         credential_type="api_key", headers={"x-video-timeout": 1800, "b": True})
            s.add(p)
            await s.commit()
            await s.refresh(p)
            assert p.headers == {"x-video-timeout": "1800", "b": "true"}
            raw = (await s.execute(text("SELECT headers FROM providers"))).scalar()
            assert json.loads(raw) == {"x-video-timeout": "1800", "b": "true"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_orm_assignment_normalizes_without_refresh(tmp_path):
    """事故时序：导入路径「先赋值、再在同会话内直接读」（不 refresh）。

    TypeDecorator 只在写库/读库时生效，故必须有 @validates 在赋值点兜底 ——
    否则导入后立刻列表仍会读到 int 值并 500。
    """
    engine, Session = _session(tmp_path)
    try:
        async with engine.begin() as c:
            await c.run_sync(Provider.metadata.create_all, tables=[Provider.__table__])
        async with Session() as s:
            p = Provider(name="t", base_url="http://x", api_type="openai_compat",
                         credential_type="api_key", headers={})
            s.add(p)
            await s.commit()
            p.headers = {"x-video-timeout": 1800, "bad": {"n": 1}}
            assert p.headers == {"x-video-timeout": "1800"}   # 未 refresh 也已干净
            await s.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_orm_constructor_normalizes(tmp_path):
    engine, Session = _session(tmp_path)
    try:
        async with engine.begin() as c:
            await c.run_sync(Provider.metadata.create_all, tables=[Provider.__table__])
        async with Session() as s:
            p = Provider(name="t", base_url="http://x", api_type="openai_compat",
                         credential_type="api_key", headers={"a": 3, "b": True})
            assert p.headers == {"a": "3", "b": "true"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_orm_update_normalizes(tmp_path):
    engine, Session = _session(tmp_path)
    try:
        async with engine.begin() as c:
            await c.run_sync(Provider.metadata.create_all, tables=[Provider.__table__])
        async with Session() as s:
            p = Provider(name="t", base_url="http://x", api_type="openai_compat",
                         credential_type="api_key", headers={})
            s.add(p)
            await s.commit()
            p.headers = {"n": 5}
            await s.commit()
            await s.refresh(p)
            assert p.headers == {"n": "5"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_orm_read_cleans_legacy_dirty_rows(tmp_path):
    """历史脏数据（绕过 ORM 写入）读取时也必须自动变干净 —— 事故当天库里就是这种行。"""
    engine, Session = _session(tmp_path)
    try:
        async with engine.begin() as c:
            await c.run_sync(Provider.metadata.create_all, tables=[Provider.__table__])
        async with Session() as s:
            s.add(Provider(name="t", base_url="http://x", api_type="openai_compat",
                           credential_type="api_key", headers={}))
            await s.commit()
        async with engine.begin() as c:
            await c.execute(text("UPDATE providers SET headers = :h"),
                            {"h": json.dumps({"x-video-timeout": 1800, "bad": {"n": 1}})})
        async with Session() as s:
            got = (await s.execute(select(Provider))).scalar()
            assert got.headers == {"x-video-timeout": "1800"}
    finally:
        await engine.dispose()


# ── 4) schema 层 ───────────────────────────────────────

def test_schema_response_tolerates_dirty_data():
    """核心回归：响应模型绝不允许因单行脏数据整体抛错（事故正是这里 500）。"""
    from types import SimpleNamespace
    from server.schemas.provider import ProviderResponse

    r = ProviderResponse.model_validate(SimpleNamespace(
        id=1, name="aistudio", base_url="http://x", api_type="openai_compat",
        credential_type="api_key",
        headers={"x-video-timeout": 1800, "bad": {"n": 1}, "ok": "v"}, description=""))
    assert r.headers == {"x-video-timeout": "1800", "ok": "v"}


def test_schema_response_keeps_none():
    from types import SimpleNamespace
    from server.schemas.provider import ProviderResponse

    r = ProviderResponse.model_validate(SimpleNamespace(
        id=1, name="a", base_url="http://x", api_type="openai_compat",
        credential_type="api_key", headers=None, description=""))
    assert r.headers is None


def test_schema_create_normalizes_ints():
    from server.schemas.provider import ProviderCreate

    c = ProviderCreate(name="a", base_url="http://x", headers={"x-video-timeout": 1800})
    assert c.headers == {"x-video-timeout": "1800"}


def test_schema_create_rejects_nested_values():
    """写入侧严格：422 让用户看到明确报错，而不是把垃圾存进库。"""
    from pydantic import ValidationError
    from server.schemas.provider import ProviderCreate

    with pytest.raises(ValidationError):
        ProviderCreate(name="a", base_url="http://x", headers={"bad": {"nested": 1}})


def test_schema_update_accepts_int_and_none():
    from server.schemas.provider import ProviderUpdate

    assert ProviderUpdate(headers={"t": 5}).headers == {"t": "5"}
    assert ProviderUpdate().headers is None      # 未提供 ≠ 清空


# ── 5) 启动存量修复 ────────────────────────────────────

def test_startup_repair_is_wired_into_init_db():
    """源码守卫：修复必须挂在 init_db 上，否则老库升级后仍然 500。"""
    import inspect
    from server import db as dbmod

    src = inspect.getsource(dbmod.init_db)
    assert "_repair_dirty_provider_headers" in src


def test_startup_repair_preserves_updated_at_and_is_dialect_safe():
    """源码守卫（两个必须守住的不变量）：

    - updated_at 显式写回自身列 → 抑制列级 onupdate，用户配置时间不被改写；
    - 走 Core update（Provider.__table__.update）而非 text() 直写 → SQLite(TEXT)
      与 PG(jsonb) 的序列化都交给 SQLAlchemy，不写死某一方言。
    """
    import inspect
    from server import db as dbmod

    body = inspect.getsource(dbmod._repair_dirty_provider_headers)
    assert "updated_at=Provider.__table__.c.updated_at" in body
    assert "Provider.__table__.update()" in body
    assert "UPDATE providers SET headers" not in body


@pytest.mark.asyncio
async def test_startup_repair_normalizes_and_is_idempotent(tmp_path, monkeypatch):
    """端到端：脏行 → 修复 → 干净；再跑一次不变；updated_at 保持原值。"""
    from server import db as dbmod

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/r.db")
    monkeypatch.setattr(dbmod, "engine", engine)
    try:
        async with engine.begin() as c:
            await c.run_sync(Provider.metadata.create_all, tables=[Provider.__table__])
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            s.add(Provider(name="dirty", base_url="http://x", api_type="openai_compat",
                           credential_type="api_key", headers={}))
            await s.commit()
        async with engine.begin() as c:
            await c.execute(text("UPDATE providers SET headers = :h, updated_at = '2026-01-01 00:00:00'"),
                            {"h": json.dumps({"x-video-timeout": 1800, "bad": {"n": 1}, "ok": "v"})})

        async def snap():
            async with engine.begin() as c:
                return (await c.execute(text(
                    "SELECT headers, updated_at FROM providers"))).fetchone()

        before = await snap()
        await dbmod._repair_dirty_provider_headers()
        after = await snap()
        assert json.loads(after[0]) == {"x-video-timeout": "1800", "ok": "v"}
        assert after[1] == before[1]           # updated_at 未被改写
        await dbmod._repair_dirty_provider_headers()
        again = await snap()
        assert again[0] == after[0]            # 幂等
    finally:
        await engine.dispose()


# ── 6) 端点级回归（事故的真实入口）─────────────────────

@pytest.mark.asyncio
async def test_list_providers_endpoint_survives_dirty_row(tmp_path, monkeypatch):
    """事故现场复现：库里有一行 int 头值，GET /admin/api/providers 必须 200 且返回全部服务商。

    修复前：ProviderResponse 校验抛 ValidationError → FastAPI 500 → 页面空白。
    """
    from server.api import admin_router

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/e.db")
    try:
        async with engine.begin() as c:
            await c.run_sync(Provider.metadata.create_all, tables=[Provider.__table__])
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            s.add(Provider(name="aistudio", base_url="http://x", api_type="openai_compat",
                           credential_type="api_key", headers={}))
            await s.commit()
        # 绕过 ORM 写入事故同款脏值
        async with engine.begin() as c:
            await c.execute(text("UPDATE providers SET headers = :h"),
                            {"h": json.dumps({"x-video-timeout": 1800})})

        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            out = await admin_router.list_providers(db=s)
        assert len(out) == 1
        assert out[0].headers == {"x-video-timeout": "1800"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_provider_endpoint_normalizes(tmp_path):
    """写入侧端点：int 头值被归一化后入库（不再制造脏数据）。"""
    from server.api import admin_router
    from server.schemas.provider import ProviderCreate

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/c.db")
    try:
        async with engine.begin() as c:
            await c.run_sync(Provider.metadata.create_all, tables=[Provider.__table__])
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            r = await admin_router.create_provider(
                ProviderCreate(name="v", base_url="http://x",
                               headers={"x-video-timeout": 1800}), db=s)
        assert r.headers == {"x-video-timeout": "1800"}
    finally:
        await engine.dispose()


# ── 7) 出站适配器实测（事故的第二现场：推理请求全挂）──

def test_openai_compat_headers_are_httpx_safe():
    """aistudio 的真实报错：TypeError: Header value must be str or bytes, not <class 'int'>。"""
    from server.adapters.openai_compat import OpenAICompatAdapter

    a = OpenAICompatAdapter()
    h = a._get_headers("k", {"__oauth": True, "x-video-timeout": 1800},
                       "https://x.invalid/v1", url="https://x.invalid/v1/chat/completions",
                       method="POST")
    assert h["x-video-timeout"] == "1800"
    assert "__oauth" not in h
    httpx.Request("POST", "https://x.invalid/v1/chat/completions", headers=h)


def test_all_adapters_strip_internal_keys_and_strify():
    """所有出站适配器统一走净化：__oauth(bool) 曾会直接抛 TypeError。"""
    from server.adapters.anthropic_adapter import AnthropicAdapter
    from server.adapters.codex_responses import CodexResponsesAdapter
    from server.adapters.github_adapter import GitHubAdapter
    from server.adapters.image_adapter import ImageAdapter
    from server.adapters.video_adapter import VideoAdapter
    from server.schemas.chat import ChatCompletionRequest

    dirty = {"__oauth": True, "__proxy_force": True, "__fg": "0",
             "__baseUrl": "https://x.invalid", "x-video-timeout": 1800}
    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    for hdrs in (
        AnthropicAdapter()._get_headers("k", dict(dirty)),
        # codex 的 _get_headers 需要 request（用于生成 session_id）
        CodexResponsesAdapter()._get_headers("k", req, dict(dirty)),
        GitHubAdapter()._get_headers("k", dict(dirty)),
        ImageAdapter()._get_headers("k", dict(dirty)),
        VideoAdapter()._get_headers("k", dict(dirty)),
    ):
        assert not [k for k in hdrs if k.startswith("__")], hdrs
        assert hdrs.get("x-video-timeout") == "1800"
        httpx.Request("POST", "https://x.invalid/v1/x", headers=hdrs)


def test_codex_keeps_official_client_header():
    """净化不能误伤适配器自己声明的头（X-Codex-Client 是上游放行判据）。"""
    from server.adapters.codex_responses import CodexResponsesAdapter
    from server.schemas.chat import ChatCompletionRequest

    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    h = CodexResponsesAdapter()._get_headers("k", req, {"__oauth": True})
    assert h["X-Codex-Client"] == "official"
