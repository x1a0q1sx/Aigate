# -*- coding: utf-8 -*-
"""F37（2026-10-13）：CodeBuddy 在线目录接入（step-5-preview 刷新看不到的修复）。

背景：`adapter.list_models` 对 CodeBuddy 家族必 404（无 /v2/models），旧逻辑直接落
静态种子 → 官方上新模型（step-5-preview）刷新永远看不到。修复 = 目录端点
（scoped ∪ /v3/config）拉取 + 与静态种子**并集**（种子只补不删，保档位别名）。

测试重点（全部有生产探针依据，见 docs/findings.md F37）：
- 两端点并集、字段映射（context/max_output/vision/name）
- 死条目（11102）、生图条目（11103）、effort 强依赖（glm-5.0-turbo，11150）过滤
- 端点级容错（一端挂用另一端；全挂回退种子）
- 刷新链路：目录 ∪ 种子（别名不丢）+ 倍率 overlay 照常
"""
import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.adapters.base_adapter import ModelInfo
from server.models.base import Base
from server.models.provider import Provider
from server.models.model import Model
from server.models.model_refresh_log import ModelRefreshLog
from server.models.request_log import RequestLog
from server.core.model_catalog import (
    ModelCatalog,
    fetch_codebuddy_catalog,
    _codebuddy_entry_to_info,
)


def _patch_httpx(monkeypatch, handler):
    """把 model_catalog 里用到的 httpx.AsyncClient 换成 handler(url) -> (status, json)。"""
    import httpx

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._d = payload
            self.content = b"{}"

        def json(self):
            return self._d

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            status, payload = handler(url)
            return _Resp(status, payload)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def _entry(mid, **kw):
    base = {"id": mid, "name": mid, "maxInputTokens": 200000,
            "maxOutputTokens": 32000, "supportsImages": True}
    base.update(kw)
    return base


class TestFetchCatalog:
    def test_union_merges_both_endpoints(self, monkeypatch):
        def handler(url):
            if "personal/models" in url:
                return 200, {"data": {"models": [_entry("a"), _entry("b")]}}
            return 200, {"data": {"models": [_entry("b", name="B2"), _entry("c")]}}

        _patch_httpx(monkeypatch, handler)
        out = asyncio.run(fetch_codebuddy_catalog("tok", "https://copilot.tencent.com"))
        ids = sorted(mi.model_id for mi in out)
        assert ids == ["a", "b", "c"]
        # 先到先得：scoped 在前，b 的 name 以 scoped 版本为准
        b = next(mi for mi in out if mi.model_id == "b")
        assert b.display_name == "b"

    def test_field_mapping(self, monkeypatch):
        def handler(url):
            if "personal/models" in url:
                return 200, {"data": {"models": [
                    _entry("m1", name="M One", maxInputTokens=1000000,
                           maxOutputTokens=64000, supportsImages=True),
                    _entry("m2", name="M Two", maxInputTokens=56000,
                           maxOutputTokens=24000, supportsImages=False),
                ]}}
            return 200, {"data": {"models": []}}

        _patch_httpx(monkeypatch, handler)
        out = {mi.model_id: mi for mi in asyncio.run(
            fetch_codebuddy_catalog("tok", "https://copilot.tencent.com"))}
        assert out["m1"].context_length == 1000000
        assert out["m1"].max_output_tokens == 64000
        assert out["m1"].supports_vision is True
        assert out["m1"].input_modalities == ["text", "image"]
        assert out["m1"].display_name == "M One"
        assert out["m2"].supports_vision is False
        assert out["m2"].input_modalities is None      # 未声明≠仅文本
        assert out["m2"].context_length == 56000

    def test_filters_delisted_ids(self, monkeypatch):
        """死条目（11102）不得进列表——上游目录清理滞后于真实下架。"""
        def handler(url):
            return 200, {"data": {"models": [
                _entry("glm-4.6"), _entry("glm-4.7"), _entry("glm-5.0"),
                _entry("minimax-m2.5"), _entry("hy4-preview-x"),
                _entry("kimi-k2-thinking"), _entry("lite"),
                _entry("step-5-preview"),
            ]}}

        _patch_httpx(monkeypatch, handler)
        out = asyncio.run(fetch_codebuddy_catalog("tok", "https://copilot.tencent.com"))
        assert [mi.model_id for mi in out] == ["step-5-preview"]

    def test_filters_image_models(self, monkeypatch):
        """生图条目（11103 聊天端点不支持）不得挂进聊天服务商。"""
        def handler(url):
            return 200, {"data": {"models": [
                _entry("hunyuan-image-alpha", tags=["text-to-image"]),
                _entry("hunyuan-image-alpha-edit", tags=["image-to-image"]),
                _entry("chat-model"),
            ]}}

        _patch_httpx(monkeypatch, handler)
        out = asyncio.run(fetch_codebuddy_catalog("tok", "https://copilot.tencent.com"))
        assert [mi.model_id for mi in out] == ["chat-model"]

    def test_filters_needs_effort_model(self, monkeypatch):
        """glm-5.0-turbo 不带 reasoning_effort 恒 11150（重测三次），过滤。"""
        def handler(url):
            return 200, {"data": {"models": [_entry("glm-5.0-turbo"), _entry("ok-model")]}}

        _patch_httpx(monkeypatch, handler)
        out = asyncio.run(fetch_codebuddy_catalog("tok", "https://copilot.tencent.com"))
        assert [mi.model_id for mi in out] == ["ok-model"]

    def test_one_endpoint_down_other_works(self, monkeypatch):
        """scoped 挂（Intl 500 实测）→ 仅 config 结果也要返回。"""
        def handler(url):
            if "personal/models" in url:
                return 500, None
            return 200, {"data": {"models": [_entry("only-config")]}}

        _patch_httpx(monkeypatch, handler)
        out = asyncio.run(fetch_codebuddy_catalog("tok", "https://www.codebuddy.ai"))
        assert [mi.model_id for mi in out] == ["only-config"]

    def test_both_down_returns_empty(self, monkeypatch):
        def handler(url):
            return 500, None

        _patch_httpx(monkeypatch, handler)
        assert asyncio.run(
            fetch_codebuddy_catalog("tok", "https://copilot.tencent.com")) == []

    def test_transport_exception_is_swallowed(self, monkeypatch):
        import httpx

        class _BadClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx, "AsyncClient", _BadClient)
        assert asyncio.run(
            fetch_codebuddy_catalog("tok", "https://copilot.tencent.com")) == []

    def test_invalid_base_returns_empty(self):
        assert asyncio.run(fetch_codebuddy_catalog("tok", "")) == []
        assert asyncio.run(fetch_codebuddy_catalog("tok", "not-a-url")) == []

    def test_only_get_requests(self, monkeypatch):
        """只读守卫：抓取过程不得出现 POST。"""
        import inspect
        import server.core.model_catalog as mc
        src = inspect.getsource(mc.fetch_codebuddy_catalog)
        assert ".get(" in src and ".post(" not in src
        assert "_CODEBUDDY_SCOPED_PATH" in src and "_CODEBUDDY_CONFIG_PATH" in src

    def test_entry_without_id_is_skipped(self):
        assert _codebuddy_entry_to_info({}) is None
        assert _codebuddy_entry_to_info({"id": ""}) is None
        assert _codebuddy_entry_to_info({"id": 123}) is None

    def test_reasoning_block_marks_effort_true(self):
        mi = _codebuddy_entry_to_info(_entry("m", reasoning={"supportedEfforts": ["low"]}))
        assert mi.supports_reasoning_effort is True
        # 无 reasoning 块 → None（未知），由名称启发式兜底
        mi2 = _codebuddy_entry_to_info(_entry("m2"))
        assert mi2.supports_reasoning_effort is None

    def test_credits_decide_free_and_ratio(self):
        """目录自带的 credits 就是权威倍率——x0.00 必须标免费（2026-10-20 实测）。"""
        assert _codebuddy_entry_to_info(_entry("free", credits="x0.00")).is_free is True
        assert _codebuddy_entry_to_info(_entry("f2", credits="x0.00 credits")).price_ratio == 0.0
        paid = _codebuddy_entry_to_info(_entry("paid", credits="x0.79 credits"))
        assert paid.is_free is False and paid.price_ratio == 0.79
        # 无 credits → 倍率未知（None），绝不能猜 0 = 免费
        unknown = _codebuddy_entry_to_info(_entry("n/a"))
        assert unknown.price_ratio is None and unknown.is_free is False

    def test_badge_tags_become_promotion(self):
        """tags 里的 badge:限时免费 / badge:夜间免费 → promotion 元数据（旧实现全丢）。"""
        mi = _codebuddy_entry_to_info(_entry("step", credits="x0.00",
                                             tags=["craft", "badge:独家限免:#009273"]))
        assert mi.promotion is not None
        assert mi.promotion["badge"]["zh"] == "独家限免"
        assert mi.promotion["active"] is True
        # 非夜间活动不挂时间窗（避免被一键套成「仅夜间可用」）
        assert "window_start" not in mi.promotion

        night = _codebuddy_entry_to_info(_entry("hy4", credits="x0.29",
                                                tags=["badge:夜间免费:#FF0000"]))
        assert night.promotion["window_start"] == "23:00"
        assert night.promotion["window_end"] == "08:00"
        assert night.promotion["timezone"] == "Asia/Shanghai"

        # 没有 badge 标签 → 不臆造促销
        assert _codebuddy_entry_to_info(_entry("plain", tags=["craft"])).promotion is None


async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cb.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[Provider.__table__, Model.__table__,
                                    ModelRefreshLog.__table__, RequestLog.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        p = Provider(name="cb", base_url="https://copilot.tencent.com/v2/chat/completions",
                     enabled=True)
        db.add(p)
        await db.commit()
        pid = p.id
    return engine, Session, pid


@pytest.mark.asyncio
async def test_refresh_union_catalog_with_seed(tmp_path, monkeypatch):
    """刷新链路：目录模型入列表 + 种子别名保留（只补不删）+ 倍率 overlay 生效。"""
    engine, Session, pid = await _setup(tmp_path)
    async with Session() as db:
        p = await db.get(Provider, pid)
        p.credential_type = "oauth"
        p.oauth_code = "codebuddy_cn"
        await db.commit()

    from server.core import oauth_client as oc_mod
    from server.core import model_catalog as mc

    class _TokClient:
        async def pick_access_token(self, code, session, owner="__default"):
            return "tok"

    monkeypatch.setattr(oc_mod, "get_oauth_client", lambda: _TokClient())

    async def _catalog(token, base_url):
        return [
            ModelInfo(model_id="step-5-preview", display_name="Step-5-Preview",
                      context_length=1000000, max_output_tokens=64000,
                      supports_vision=True),
            ModelInfo(model_id="deepseek-v4-pro", display_name="Deepseek-V4-Pro"),
        ]

    async def _ratios(token, base_url):
        return {"step-5-preview": 0.0, "deepseek-v4-pro": 0.51}

    async def _no_pricing(base_url, timeout=None):
        class _R:
            pricing = {}
            source_url = ""
            error = ""
        return _R()

    monkeypatch.setattr(mc, "fetch_codebuddy_catalog", _catalog)
    monkeypatch.setattr(mc, "fetch_codebuddy_ratios", _ratios)
    monkeypatch.setattr(mc, "fetch_provider_pricing", _no_pricing)

    cat = ModelCatalog()
    async with Session() as db:
        p = await db.get(Provider, pid)
        r = await cat.refresh_models_from_provider(db, p, None)
        assert "error" not in r, r
        assert r["list_source"] == "online"
        assert "在线目录" in (r["list_note"] or "")
        assert "种子补充" in (r["list_note"] or "")

    async with Session() as db:
        rows = {m.model_id: m for m in (await db.execute(
            select(Model).where(Model.provider_id == pid))).scalars().all()}
        # 目录新模型入库
        step5 = rows["step-5-preview"]
        assert step5.price_ratio == 0.0
        assert step5.is_free is True
        assert step5.price_ratio_source == "codebuddy"
        assert step5.context_length == 1000000
        assert step5.supports_vision is True
        # 种子别名保留（目录不含 auto/balanced-model，不能删）
        assert "auto" in rows
        assert "balanced-model" in rows
        # 目录已有模型不被种子覆盖（deepseek-v4-pro 走目录 + 倍率）
        assert rows["deepseek-v4-pro"].price_ratio == 0.51


@pytest.mark.asyncio
async def test_refresh_seed_fallback_when_catalog_empty(tmp_path, monkeypatch):
    """两端点全挂 → 仍回退静态种子（list_source=seed，流程不回归）。"""
    engine, Session, pid = await _setup(tmp_path)
    async with Session() as db:
        p = await db.get(Provider, pid)
        p.credential_type = "oauth"
        p.oauth_code = "codebuddy_cn"
        await db.commit()

    from server.core import oauth_client as oc_mod
    from server.core import model_catalog as mc

    class _TokClient:
        async def pick_access_token(self, code, session, owner="__default"):
            return "tok"

    monkeypatch.setattr(oc_mod, "get_oauth_client", lambda: _TokClient())

    async def _empty(token, base_url):
        return []

    async def _no_ratios(token, base_url):
        return {}

    async def _no_pricing(base_url, timeout=None):
        class _R:
            pricing = {}
            source_url = ""
            error = ""
        return _R()

    monkeypatch.setattr(mc, "fetch_codebuddy_catalog", _empty)
    monkeypatch.setattr(mc, "fetch_codebuddy_ratios", _no_ratios)
    monkeypatch.setattr(mc, "fetch_provider_pricing", _no_pricing)

    cat = ModelCatalog()
    async with Session() as db:
        p = await db.get(Provider, pid)
        r = await cat.refresh_models_from_provider(db, p, None)
        assert "error" not in r, r
        assert r["list_source"] == "seed"
        assert "种子" in (r["list_note"] or "")

    async with Session() as db:
        rows = {m.model_id for m in (await db.execute(
            select(Model).where(Model.provider_id == pid))).scalars().all()}
        assert "step-5-preview" in rows       # 种子已含新模型
        assert "auto" in rows


class TestSeedContainsNewModels:
    """种子表与在线目录一致性：新实测模型必须在种子（离线兜底也看得到）。"""

    def test_cn_seed_has_new_models(self):
        from server.core.oauth_registry import get_oauth_provider
        cn = {m["model_id"] for m in get_oauth_provider("codebuddy_cn").static_models}
        for mid in ("step-5-preview", "space-bunny", "hy3-x",
                    "hunyuan-2.0-thinking", "hunyuan-chat", "default"):
            assert mid in cn, f"CN 种子缺 {mid}"

    def test_cn_seed_has_no_delisted(self):
        """已实测 11102 下架的不许留在种子（与倍率表同规矩）。"""
        from server.core.oauth_registry import get_oauth_provider
        cn = {m["model_id"] for m in get_oauth_provider("codebuddy_cn").static_models}
        for gone in ("glm-4.6", "glm-4.7", "glm-5.0", "minimax-m2.5",
                     "hy4-preview-x", "kimi-k2-thinking", "glm-5.0-turbo"):
            assert gone not in cn, f"{gone} 已下架/不可用，不应留在 CN 种子"

    def test_step5_ratio_zero_and_free(self):
        from server.core.oauth_registry import STATIC_PRICE_RATIOS
        assert STATIC_PRICE_RATIOS["codebuddy_cn"]["step-5-preview"] == 0.0
