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
    """在线 471 条公共目录不含 cline-pass/*；刷新后订阅模型仍要在库里。"""
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
        # 公共目录：只有按量计费模型，没有 cline-pass/*
        return [
            ModelInfo(model_id="anthropic/claude-opus-4.7", display_name="Opus 4.7"),
            ModelInfo(model_id="moonshotai/kimi-k3", display_name="kimi-k3 (PAYG)"),
        ]

    from server.adapters.openai_compat import OpenAICompatAdapter
    monkeypatch.setattr(OpenAICompatAdapter, "list_models", _list_models)

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
    # 在线目录模型入库
    assert "anthropic/claude-opus-4.7" in rows
    assert "moonshotai/kimi-k3" in rows
    # 订阅专有模型由种子补齐（在线目录没有）
    assert "cline-pass/kimi-k3" in rows
    assert "cline-pass/glm-5.3-flash" in rows
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
        return [ModelInfo(model_id="anthropic/claude-opus-4.7",
                          display_name="Opus 4.7 (online)",
                          context_length=200000)]

    from server.adapters.openai_compat import OpenAICompatAdapter
    monkeypatch.setattr(OpenAICompatAdapter, "list_models", _list_models)

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
    assert rows["anthropic/claude-opus-4.7"].display_name == "Opus 4.7 (online)"
    assert rows["anthropic/claude-opus-4.7"].context_length == 200000
    await engine.dispose()
