# -*- coding: utf-8 -*-
"""定价兜底加固（2026-10-09 tokenharbor 事故回归）：
1) 文本兜底不得把营销页 SVG 坐标/纯数字当模型名；
2) 刷新路径跟随服务商 proxy_enabled（与推理侧对齐，否则国别封锁站点永远拉不到在线列表）；
3) 库里已有模型时，在线列表失败绝不做「定价页猜名」兜底重建；兜底建模 list_source 记 pricing。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.base import Base
from server.models.provider import Provider
from server.models.model import Model
from server.models.api_key import ApiKey
from server.models.model_api_key import ModelApiKey
from server.models.model_refresh_log import ModelRefreshLog
from server.adapters.xyusec_pricing import _extract_pricing_from_text, PricingSyncResult
from server.core.model_catalog import ModelCatalog


# ── 1) 文本兜底解析 ──────────────────────────────────────────────

def test_text_fallback_rejects_svg_coordinates_and_numeric_keys():
    html = (
        '<svg width="640 780" height="12 34"><path d="M640 780 L856 1156 l645 220 175"/></svg>'
        '<span>gpt-4o</span><span>$2.5</span><span>$10</span>'
        '<p>claude-sonnet-5 $1 $5</p>'
        '<b>124 0.5 1.5</b>'
    )
    pricing = _extract_pricing_from_text(html)
    assert "gpt-4o" in pricing and "claude-sonnet-5" in pricing
    # 纯数字与「单字母+多位数字」（SVG 路径命令+坐标）一律不得成为模型键
    assert not any(k.isdigit() for k in pricing), pricing
    assert not any(k in ("640", "856", "l645", "220", "175") for k in pricing), pricing


def test_text_fallback_svg_block_fully_removed():
    # 剥掉 <svg> 整块后，块内再也不会产生可匹配的残片
    svg_only = "<svg>" + " ".join(f"{i}{j} 1.5 2.5" for i in range(3) for j in range(30)) + "</svg>"
    assert _extract_pricing_from_text(svg_only) == {}


# ── 2)/3) 刷新路径行为 ──────────────────────────────────────────

class FakeKeyManager:
    class _Crypto:
        @staticmethod
        def decrypt(_enc):
            return "sk-fake"
    _crypto = _Crypto()


class FakeAdapter:
    def __init__(self, fail=False, models=None, sink=None):
        self._fail = fail
        self._models = models or []
        self._sink = sink if sink is not None else []

    async def list_models(self, api_key, base_url, extra_headers=None):
        self._sink.append(extra_headers)
        if self._fail:
            raise RuntimeError("HTTP 403 region_blocked")
        return self._models


async def _setup(tmp_path, **prov_kw):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pf.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[
            Provider.__table__, Model.__table__, ApiKey.__table__,
            ModelApiKey.__table__, ModelRefreshLog.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        p = Provider(name="pf-provider", base_url="https://x.example/v1",
                     api_type="openai_compat", enabled=True, **prov_kw)
        db.add(p)
        await db.commit()
        pid = p.id
        db.add(ApiKey(provider_id=pid, key_encrypted="enc", key_prefix="sk", is_active=True))
        await db.commit()
    return engine, Session, pid


@pytest.mark.asyncio
async def test_refresh_forces_proxy_when_provider_enabled(tmp_path, monkeypatch):
    engine, Session, pid = await _setup(tmp_path, proxy_enabled=True)
    sink = []
    monkeypatch.setattr("server.core.model_catalog.create_adapter_for_provider",
                        lambda api_type, timeout=None: FakeAdapter(models=[], sink=sink))
    monkeypatch.setattr("server.core.model_catalog.fetch_provider_pricing",
                        _no_pricing)
    try:
        async with Session() as db:
            p = await db.get(Provider, pid)
            await ModelCatalog().refresh_models_from_provider(db, p, FakeKeyManager())
            # 服务商自定义头不被污染（内部标记只存在于拷贝）
            assert "__proxy_force" not in (p.headers or {})
        assert sink and sink[0] and sink[0].get("__proxy_force") is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pricing_fallback_skipped_when_models_exist(tmp_path, monkeypatch):
    engine, Session, pid = await _setup(tmp_path)
    # 库里已有 1 个真实模型
    async with Session() as db:
        db.add(Model(provider_id=pid, model_id="claude-real", display_name="Claude Real",
                     enabled=True, auto_enabled=False, is_free=False,
                     supports_streaming=True, priority_boost=0, auto_excluded=False,
                     is_manual=False))
        await db.commit()
    monkeypatch.setattr("server.core.model_catalog.create_adapter_for_provider",
                        lambda api_type, timeout=None: FakeAdapter(fail=True))
    monkeypatch.setattr("server.core.model_catalog.fetch_provider_pricing",
                        _svg_pricing)
    try:
        async with Session() as db:
            p = await db.get(Provider, pid)
            r = await ModelCatalog().refresh_models_from_provider(db, p, FakeKeyManager())
        assert r["added"] == 0 and r["removed"] == 0
        assert r["list_source"] is None
        assert "不做定价页猜名兜底" in (r["list_note"] or "")
        async with Session() as db:
            rows = (await db.execute(select(Model).where(Model.provider_id == pid))).scalars().all()
            assert [m.model_id for m in rows] == ["claude-real"]  # SVG 假名没有进来
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pricing_fallback_creates_and_logs_pricing_source(tmp_path, monkeypatch):
    engine, Session, pid = await _setup(tmp_path)  # 空库
    monkeypatch.setattr("server.core.model_catalog.create_adapter_for_provider",
                        lambda api_type, timeout=None: FakeAdapter(fail=True))
    monkeypatch.setattr("server.core.model_catalog.fetch_provider_pricing",
                        _svg_pricing)
    try:
        async with Session() as db:
            p = await db.get(Provider, pid)
            r = await ModelCatalog().refresh_models_from_provider(db, p, FakeKeyManager())
        assert r["added"] == 2
        assert r["list_source"] == "pricing"
        async with Session() as db:
            rows = (await db.execute(select(Model).where(Model.provider_id == pid))).scalars().all()
            assert {m.model_id for m in rows} == {"gpt-9", "gpt-9-mini"}
            logs = (await db.execute(select(ModelRefreshLog))).scalars().all()
            assert logs and logs[0].list_source == "pricing"
    finally:
        await engine.dispose()


# ── 4) 统一凭证解析器：标准 api_key 路径同样合并 proxy_enabled ──

@pytest.mark.asyncio
async def test_resolver_standard_path_merges_proxy_force(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from server.core import credential_resolver as cr

    class FakeCrypto:
        @staticmethod
        def decrypt(_enc):
            return "sk-fake"

    class FakeRotator:
        async def pick_key_for_model(self, db, model):
            return None

    monkeypatch.setattr("server.core.crypto_service.get_crypto_service", lambda: FakeCrypto())
    monkeypatch.setattr("server.core.key_rotator.get_key_rotator", lambda: FakeRotator())
    engine, Session, pid = await _setup(tmp_path, proxy_enabled=True,
                                        headers={"X-Route-Tag": "th"})
    try:
        async with Session() as db:
            p = await db.get(Provider, pid)
            rc = await cr.resolve_credential_async(
                p, SimpleNamespace(model_id="apex", id=1), db)
        assert rc.ok and rc.api_key == "sk-fake"
        eh = rc.extra_headers or {}
        assert eh.get("__proxy_force") is True
        assert eh.get("X-Route-Tag") == "th"
        assert "__oauth" not in eh
        # 关掉开关则不应带 force 标记
        async with Session() as db:
            p = await db.get(Provider, pid)
            p.proxy_enabled = False
            await db.commit()
            rc2 = await cr.resolve_credential_async(
                p, SimpleNamespace(model_id="apex", id=1), db)
        assert "__proxy_force" not in (rc2.extra_headers or {})
    finally:
        await engine.dispose()


async def _no_pricing(base_url, timeout=None):
    return PricingSyncResult({}, "https://x.example", None)


async def _svg_pricing(base_url, timeout=None):
    return PricingSyncResult(
        {"gpt-9": {"input": 1.0, "output": 2.0, "is_free": False},
         "gpt-9-mini": {"input": 0.1, "output": 0.2, "is_free": False}},
        "https://x.example", None)
