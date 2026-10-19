# -*- coding: utf-8 -*-
"""Cline / ClinePass 接入（2026-10-18）。

生产实证（docs/progress.md Session 28）：
- Cline OAuth token 是 WorkOS JWT，**额度/账户端点必须 `Bearer workos:<jwt>`**；
  裸 Bearer 一律 401 "Please make sure you're using the latest version of Cline"。
  clp_ API key 则相反：原样发送，加 workos: 会 401。
- 额度 = 三个滚动窗口的百分比（`/users/me/plan/usage-limits`），
  绝对上限在 `/users/me/plan` 的 `inferenceCapThreshold`（单位 1e-8 USD）。
- `/api/v1/models` 的 471 条公共目录**不含** `cline-pass/*` 订阅专有 ID，
  但订阅账号推理 `cline-pass/<name>` 200 → 刷新必须「在线目录 ∪ 种子」（只补不删），
  否则每次刷新都会把订阅模型洗掉。
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.adapters.base_adapter import ModelInfo
from server.models.base import Base
from server.models.provider import Provider
from server.models.model import Model
from server.models.model_refresh_log import ModelRefreshLog
from server.models.request_log import RequestLog


# ── 注册表种子 ───────────────────────────────────────

def test_cline_seed_contains_pass_models():
    """订阅专有 cline-pass/* 必须在种子（公共目录拉不到，只能靠种子补）。"""
    from server.core.oauth_registry import get_oauth_provider
    ids = {m["model_id"] for m in get_oauth_provider("cline").static_models}
    for mid in (
        "cline-pass/kimi-k3",
        "cline-pass/qwen3.8-max",
        "cline-pass/qwen3.7-max",
        "cline-pass/qwen3.7-plus",
        "cline-pass/glm-5.3",
        "cline-pass/glm-5.3-flash",
        "cline-pass/deepseek-v4-pro",
        "cline-pass/deepseek-v4.1-flash",
        "cline-pass/minimax-m3",
        "cline-pass/mimo-v2.5-pro",
        "cline-pass/mimo-v2.5",
        "cline-pass/muse-spark-1.3-contributor",
    ):
        assert mid in ids, f"cline 种子缺订阅模型 {mid}"


def test_cline_seed_keeps_payg_models():
    """按量计费公共目录模型的种子也要保留（在线失败时的兜底）。"""
    from server.core.oauth_registry import get_oauth_provider
    ids = {m["model_id"] for m in get_oauth_provider("cline").static_models}
    for mid in ("anthropic/claude-opus-4.7", "openai/gpt-5.4",
                "google/gemini-3.1-pro-preview", "kwaipilot/kat-coder-pro"):
        assert mid in ids


# ── 客户端头族（2026-10-18 A/B：X-CLIENT-TYPE 是产品面闸判据）─────────

def test_cline_quirks_require_client_type_header():
    """cline-free/*、cline-cloud/* 必须带 X-CLIENT-TYPE: cline-sdk（缺则 403）。"""
    from server.core.provider_quirks import quirks_for, cline_client_headers
    q = quirks_for("https://api.cline.bot/api/v1/chat/completions")
    assert q is not None and q.cline_client_headers is True
    h = cline_client_headers("tok", {"messages": [{"role": "user", "content": "hi"}]})
    assert h["X-CLIENT-TYPE"] == "cline-sdk"
    assert h["X-CLIENT-VERSION"] == "3.0.47"
    assert h["X-PLATFORM"] == "terminal"
    # 会话 id 按（账号 × 首条用户消息）稳定派生：同账号同消息 → 同 id
    h2 = cline_client_headers("tok", {"messages": [{"role": "user", "content": "hi"}]})
    assert h["X-Task-ID"] == h2["X-Task-ID"]
    # 换账号 / 换消息 → 不同 id（多账号池不共用会话 id）
    h3 = cline_client_headers("tok2", {"messages": [{"role": "user", "content": "hi"}]})
    assert h3["X-Task-ID"] != h["X-Task-ID"]


def test_cline_headers_injected_into_outbound_request():
    """适配器出站头必须包含 Cline 产品面头（静态回归）。"""
    from pathlib import Path
    src = Path("server/adapters/openai_compat.py").read_text(encoding="utf-8")
    assert "cline_client_headers" in src
    assert "q.cline_client_headers" in src


# ── 在线推荐目录（free/clinePass 分组）────────────────────────────

def test_cline_recommended_catalog_parses_groups(monkeypatch):
    """fetch_cline_catalog 解析 free/clinePass/recommended 三组并保留前缀。"""
    import asyncio
    import httpx
    from server.core import model_catalog as mc

    class _Resp:
        status_code = 200
        def json(self):
            return {
                "recommended": [{"id": "anthropic/claude-sonnet-5.5", "name": "claude-sonnet-5.5"}],
                "free": [{"id": "cline-free/solar-mini4", "name": "Solar Mini 4"}],
                "clinePass": [{"id": "cline-pass/kimi-k3", "name": "Kimi K3"}],
            }

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _Client())
    out = asyncio.run(mc.fetch_cline_catalog("tok", "https://api.cline.bot/api/v1"))
    by_id = {m.model_id: m for m in out}
    assert "cline-free/solar-mini4" in by_id
    assert by_id["cline-free/solar-mini4"].is_free is True
    assert "cline-pass/kimi-k3" in by_id
    assert " (Pass)" in by_id["cline-pass/kimi-k3"].display_name
    assert "anthropic/claude-sonnet-5.5" in by_id


# ── 刷新链路：在线目录 ∪ 种子 ─────────────────────────

async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cline.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[Provider.__table__, Model.__table__,
                                    ModelRefreshLog.__table__, RequestLog.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        p = Provider(name="cline", base_url="https://api.cline.bot/api/v1/chat/completions",
                     enabled=True)
        db.add(p)
        await db.commit()
        pid = p.id
    return engine, Session, pid


@pytest.mark.asyncio
async def test_cline_refresh_unions_online_with_pass_seed(tmp_path, monkeypatch):
    """在线公共目录（按量计费）不得入库；订阅/免费模型由种子补齐。"""
    engine, Session, pid = await _setup(tmp_path)
    async with Session() as db:
        p = await db.get(Provider, pid)
        p.credential_type = "oauth"
        p.oauth_code = "cline"
        await db.commit()

    from server.core import oauth_client as oc_mod
    from server.core import model_catalog as mc

    class _TokClient:
        async def pick_access_token(self, code, session, owner="__default"):
            return "tok"

    monkeypatch.setattr(oc_mod, "get_oauth_client", lambda: _TokClient())

    async def _list_models(self, api_key, base_url, extra_headers=None):
        # 公共目录：按量计费模型（余额 $0 → 402）+ 一条 :free 免费模型
        return [
            ModelInfo(model_id="anthropic/claude-opus-4.7", display_name="Opus 4.7"),
            ModelInfo(model_id="moonshotai/kimi-k3", display_name="kimi-k3 (PAYG)"),
            ModelInfo(model_id="cohere/north-mini-code:free", display_name="North Mini (Free)"),
        ]

    from server.adapters.openai_compat import OpenAICompatAdapter
    monkeypatch.setattr(OpenAICompatAdapter, "list_models", _list_models)

    async def _rec_catalog(token, base_url):
        return [ModelInfo(model_id="cline-free/solar-mini4", display_name="Solar Mini 4 (Free)"),
                ModelInfo(model_id="cline-pass/kimi-k3", display_name="Kimi K3 (Pass)"),
                ModelInfo(model_id="cline-pass/muse-spark-1.3-contributor",
                          display_name="Muse Spark (Pass)")]

    monkeypatch.setattr(mc, "fetch_cline_catalog", _rec_catalog)

    async def _no_pricing(base_url, timeout=None):
        class _R:
            pricing = {}
            source_url = ""
            error = ""
        return _R()

    monkeypatch.setattr(mc, "fetch_provider_pricing", _no_pricing)

    cat = mc.ModelCatalog()
    async with Session() as db:
        p = await db.get(Provider, pid)
        r = await cat.refresh_models_from_provider(db, p, None)
        assert "error" not in r, r
        assert r["list_source"] == "online"
        assert "种子补充" in (r["list_note"] or ""), r["list_note"]

    async with Session() as db:
        rows = {m.model_id for m in (await db.execute(
            select(Model).where(Model.provider_id == pid))).scalars().all()}
    # 按量计费公共目录模型**不入库**（余额 $0 全族 402）
    assert "anthropic/claude-opus-4.7" not in rows
    assert "moonshotai/kimi-k3" not in rows
    # 免费后缀 / 订阅 / 官方 free 入库
    assert "cohere/north-mini-code:free" in rows
    assert "cline-pass/kimi-k3" in rows
    assert "cline-free/solar-mini4" in rows
    assert "cline-pass/glm-5.3-flash" in rows
    # 区域封锁条目（403 not available in your region）不入库
    assert "cline-pass/muse-spark-1.3-contributor" not in rows
    assert "cline-free/muse-spark-1.3-contributor" not in rows
    await engine.dispose()


@pytest.mark.asyncio
async def test_cline_refresh_online_model_wins_over_seed(tmp_path, monkeypatch):
    """同一 ID 在线与种子都有时，以在线元数据为准（种子不覆盖）。"""
    engine, Session, pid = await _setup(tmp_path)
    async with Session() as db:
        p = await db.get(Provider, pid)
        p.credential_type = "oauth"
        p.oauth_code = "cline"
        await db.commit()

    from server.core import oauth_client as oc_mod
    from server.core import model_catalog as mc

    class _TokClient:
        async def pick_access_token(self, code, session, owner="__default"):
            return "tok"

    monkeypatch.setattr(oc_mod, "get_oauth_client", lambda: _TokClient())

    async def _list_models(self, api_key, base_url, extra_headers=None):
        return [ModelInfo(model_id="cohere/north-mini-code:free",
                          display_name="North Mini (online)",
                          context_length=200000)]

    from server.adapters.openai_compat import OpenAICompatAdapter
    monkeypatch.setattr(OpenAICompatAdapter, "list_models", _list_models)

    async def _rec_catalog(token, base_url):
        return []

    monkeypatch.setattr(mc, "fetch_cline_catalog", _rec_catalog)

    async def _no_pricing(base_url, timeout=None):
        class _R:
            pricing = {}
            source_url = ""
            error = ""
        return _R()

    monkeypatch.setattr(mc, "fetch_provider_pricing", _no_pricing)

    cat = mc.ModelCatalog()
    async with Session() as db:
        p = await db.get(Provider, pid)
        r = await cat.refresh_models_from_provider(db, p, None)
        assert "error" not in r, r

    async with Session() as db:
        rows = {m.model_id: m for m in (await db.execute(
            select(Model).where(Model.provider_id == pid))).scalars().all()}
    assert rows["cohere/north-mini-code:free"].display_name == "North Mini (online)"
    assert rows["cohere/north-mini-code:free"].context_length == 200000
    await engine.dispose()


# ── 可用集合判据（2026-10-19「pass 分组不能用的都删掉」）───────────

def test_cline_model_usable_predicate():
    from server.core.model_catalog import cline_model_usable
    # 订阅专有 / 官方免费 / 公共 :free 可用
    assert cline_model_usable("cline-pass/kimi-k3") is True
    assert cline_model_usable("cline-free/solar-mini4") is True
    assert cline_model_usable("cohere/north-mini-code:free") is True
    # 按量计费公共目录（余额 $0 → 402）不可用
    assert cline_model_usable("anthropic/claude-opus-4.7") is False
    assert cline_model_usable("openai/gpt-5.1") is False
    assert cline_model_usable("deepseek/deepseek-v4.1-flash-1017") is False
    # cline-cloud 实测 403 not supported
    assert cline_model_usable("cline-cloud/deepseek-v4.1-flash") is False
    # 区域封锁条目
    assert cline_model_usable("cline-pass/muse-spark-1.3-contributor") is False
    assert cline_model_usable("") is False


def test_is_cline_provider_matches_both_channels():
    from types import SimpleNamespace
    from server.core.model_catalog import is_cline_provider
    assert is_cline_provider(SimpleNamespace(base_url="https://api.cline.bot/api/v1")) is True
    assert is_cline_provider(SimpleNamespace(
        base_url="https://api.cline.bot/api/v1/chat/completions")) is True
    assert is_cline_provider(SimpleNamespace(base_url="https://api.openai.com/v1")) is False
    assert is_cline_provider(SimpleNamespace(base_url=None)) is False


@pytest.mark.asyncio
async def test_cline_pass_api_key_refresh_keeps_only_usable(tmp_path, monkeypatch):
    """ClinePass（api_key 渠道）刷新同样只保留订阅/免费模型（公共目录 402 全删）。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cp.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[Provider.__table__, Model.__table__,
                                    ModelRefreshLog.__table__, RequestLog.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    from server.models.api_key import ApiKey
    from server.models.model_api_key import ModelApiKey
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[ApiKey.__table__, ModelApiKey.__table__])
    async with Session() as db:
        p = Provider(name="Cline Pass", base_url="https://api.cline.bot/api/v1",
                     api_type="openai_compat", credential_type="api_key", enabled=True)
        db.add(p)
        await db.commit()
        pid = p.id
        db.add(ApiKey(provider_id=pid, key_encrypted="enc:sk", key_prefix="sk_", is_active=True))
        await db.commit()

    from server.core import model_catalog as mc

    async def _list_models(self, api_key, base_url, extra_headers=None):
        return [ModelInfo(model_id="anthropic/claude-haiku-4.5", display_name="Haiku"),
                ModelInfo(model_id="nvidia/nemotron-3.5-super-vl:free", display_name="Nemotron Free")]

    from server.adapters.openai_compat import OpenAICompatAdapter
    monkeypatch.setattr(OpenAICompatAdapter, "list_models", _list_models)

    async def _rec_catalog(token, base_url):
        return [ModelInfo(model_id="cline-pass/kimi-k3", display_name="Kimi K3 (Pass)")]

    monkeypatch.setattr(mc, "fetch_cline_catalog", _rec_catalog)

    async def _no_pricing(base_url, timeout=None):
        class _R:
            pricing = {}
            source_url = ""
            error = ""
        return _R()

    monkeypatch.setattr(mc, "fetch_provider_pricing", _no_pricing)

    class _Crypto:
        @staticmethod
        def decrypt(_enc):
            return "sk-fake"

    class _KM:
        _crypto = _Crypto()

    cat = mc.ModelCatalog()
    async with Session() as db:
        p = await db.get(Provider, pid)
        r = await cat.refresh_models_from_provider(db, p, _KM())
        assert "error" not in r, r

    async with Session() as db:
        rows = {m.model_id for m in (await db.execute(
            select(Model).where(Model.provider_id == pid))).scalars().all()}
    assert "cline-pass/kimi-k3" in rows
    assert "nvidia/nemotron-3.5-super-vl:free" in rows
    assert "anthropic/claude-haiku-4.5" not in rows
    await engine.dispose()
