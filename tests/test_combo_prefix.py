"""combo 前缀路由回归测试。

新寻址模式：/combo:<名称或id>/v1/... 直接锁定组合，
    GET  /combo:918/v1/models 及简写 /combo:918/models → 该组合候选模型列表
    POST /combo:918/v1/chat/completions（及 /v1/messages /v1/responses）
        → model 字段被强制改写为 combo:<名称>，走组合级联回退。
"""
import asyncio
import types

import pytest


# ── 中间件：路径剥前缀 + state 注入 ──────────────────────────

def _build_app():
    from fastapi import FastAPI, Request
    from server.core.combo_prefix import ComboPrefixMiddleware
    app = FastAPI()
    app.add_middleware(ComboPrefixMiddleware)

    @app.get("/v1/models")
    async def models_endpoint(request: Request):
        return {"path": request.url.path,
                "combo": getattr(request.state, "combo_scope", None)}

    @app.post("/v1/chat/completions")
    async def chat_endpoint(request: Request):
        return {"path": request.url.path,
                "combo": getattr(request.state, "combo_scope", None)}

    return app


def _client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


@pytest.mark.parametrize("url,expect_combo", [
    ("/combo:918/v1/chat/completions", "918"),
    ("/combo:my-fast/v1/models", "my-fast"),
    ("/combo:%E4%B8%AD%E6%96%87/v1/models", "中文"),   # URL 编码的组合名
])
def test_middleware_strips_prefix_and_injects_scope(url, expect_combo):
    method = "POST" if "chat" in url else "GET"
    async def _t():
        app = _build_app()
        async with _client(app) as c:
            r = await c.request(method, url, json={} if method == "POST" else None)
            assert r.status_code == 200, r.text
            assert r.json() == {"path": "/v1/chat/completions" if method == "POST" else "/v1/models",
                                "combo": expect_combo}
    asyncio.run(_t())


@pytest.mark.parametrize("url", [
    "/combo:918", "/combo:918/", "/combo:918/models", "/combo:918/model",
    "/combo:918/v1", "/combo:918/v1/",
])
def test_models_alias_urls_map_to_v1_models(url):
    async def _t():
        app = _build_app()
        async with _client(app) as c:
            r = await c.get(url)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["path"] == "/v1/models" and body["combo"] == "918"
    asyncio.run(_t())


def test_plain_v1_unaffected_by_middleware():
    async def _t():
        app = _build_app()
        async with _client(app) as c:
            r = await c.get("/v1/models")
            assert r.status_code == 200
            assert r.json() == {"path": "/v1/models", "combo": None}
    asyncio.run(_t())


# ── find_combo_by_ref：名称优先，数字回退 id ─────────────────

def _combo_db(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        from server.models.combo import Combo
        async with engine.begin() as conn:
            await conn.run_sync(Combo.__table__.create)
    asyncio.run(_setup())
    return engine, Session


def test_find_combo_by_ref_name_and_id(tmp_path):
    from server.models.combo import Combo
    from server.core.combo_router import find_combo_by_ref
    engine, Session = _combo_db(tmp_path)

    async def _t():
        async with Session() as db:
            c1 = Combo(name="my-fast", strategy="fallback", model_ids=[], enabled=True)
            c2 = Combo(name="918号", strategy="fallback", model_ids=[], enabled=True)
            db.add_all([c1, c2])
            await db.commit()
            got = await find_combo_by_ref(db, "my-fast")
            assert got is not None and got.id == c1.id
            got2 = await find_combo_by_ref(db, str(c2.id))  # 纯数字 → 按 id
            assert got2 is not None and got2.name == "918号"
            assert await find_combo_by_ref(db, "nope") is None
            assert await find_combo_by_ref(db, "999999") is None
    asyncio.run(_t())
    asyncio.run(engine.dispose())


# ── _apply_combo_scope：model 改写语义 ───────────────────────

def _fake_raw(ref):
    st = types.SimpleNamespace()
    if ref is not None:
        st.combo_scope = ref
    return types.SimpleNamespace(state=st)


def _req(model):
    from server.schemas.chat import ChatCompletionRequest
    return ChatCompletionRequest(model=model,
                                 messages=[{"role": "user", "content": "hi"}])


def _patch_combo(monkeypatch, name):
    import server.core.combo_router as cr
    combo = types.SimpleNamespace(name=name) if name else None

    async def _find(db, ref):
        return combo
    monkeypatch.setattr(cr, "find_combo_by_ref", _find)


def test_no_scope_keeps_model(monkeypatch):
    from server.api.v1_router import _apply_combo_scope
    _patch_combo(monkeypatch, "whatever")
    r = _req("gpt-4o")
    new_r, err = asyncio.run(_apply_combo_scope(None, _fake_raw(None), r))
    assert err is None and new_r.model == "gpt-4o"


def test_scope_forces_combo_name(monkeypatch):
    from server.api.v1_router import _apply_combo_scope
    _patch_combo(monkeypatch, "my-fast")
    new_r, err = asyncio.run(_apply_combo_scope(None, _fake_raw("918"), _req("some-model")))
    assert err is None and new_r.model == "combo:my-fast"


def test_scope_overrides_explicit_combo_in_body(monkeypatch):
    from server.api.v1_router import _apply_combo_scope
    _patch_combo(monkeypatch, "my-fast")
    # 前缀是访问边界：body 里写别的 combo 也以前缀为准
    new_r, err = asyncio.run(_apply_combo_scope(None, _fake_raw("918"), _req("combo:other")))
    assert err is None and new_r.model == "combo:my-fast"


def test_scope_preserves_effort_suffix(monkeypatch):
    from server.api.v1_router import _apply_combo_scope
    _patch_combo(monkeypatch, "my-fast")
    new_r, err = asyncio.run(_apply_combo_scope(None, _fake_raw("918"), _req("deepseek-r1-high")))
    assert err is None and new_r.model == "combo:my-fast-high"


def test_scope_unknown_combo_404(monkeypatch):
    from server.api.v1_router import _apply_combo_scope
    _patch_combo(monkeypatch, None)
    new_r, err = asyncio.run(_apply_combo_scope(None, _fake_raw("999"), _req("gpt-4o")))
    assert err is not None and err.status_code == 404
    assert new_r.model == "gpt-4o"  # 未改写


# ── /v1/models 组合过滤 ──────────────────────────────────────

def test_list_models_combo_scope(tmp_path, monkeypatch):
    import time as _t
    import server.core.combo_router as cr
    from server.api.v1_router import list_models

    model = types.SimpleNamespace(
        model_id="glm-4", provider_id=1, input_price=1.0, output_price=2.0,
        cache_read_input_price=0.1, cache_write_input_price=0.2,
        is_free=False, auto_enabled=True, supports_streaming=True,
        supports_vision=True, supports_reasoning_effort=None,
        context_length=131072, created_at=None)
    provider = types.SimpleNamespace(name="zhipu")
    combo = types.SimpleNamespace(name="my-fast")

    async def _find(db, ref):
        assert ref == "918"
        return combo

    async def _targets(db, c):
        return [{"provider": provider, "model": model, "full_id": "zhipu/glm-4", "weight": None}]

    monkeypatch.setattr(cr, "find_combo_by_ref", _find)
    monkeypatch.setattr(cr, "resolve_combo_targets", _targets)

    resp = asyncio.run(list_models(raw_request=_fake_raw("918"), db=None, include_effort=False))
    assert resp["object"] == "list"
    entry = resp["data"][0]
    assert entry["id"] == "zhipu/glm-4"
    assert entry["aigate_combo"] == "my-fast"
    assert entry["owned_by"] == "zhipu"
    assert entry["pricing"]["input"] == 1.0
    assert entry["capabilities"]["context_length"] == 131072


def test_list_models_combo_scope_unknown_404(monkeypatch):
    import server.core.combo_router as cr
    from server.api.v1_router import list_models

    async def _find(db, ref):
        return None
    monkeypatch.setattr(cr, "find_combo_by_ref", _find)
    resp = asyncio.run(list_models(raw_request=_fake_raw("nope"), db=None))
    assert resp.status_code == 404
