# -*- coding: utf-8 -*-
"""u1s1 客户端证明（attestation）缓存模块：新鲜命中 / 临期后台刷新 / 首拉阻塞 / 失败冷却。"""
import asyncio
import time

import pytest

from server.core import u1s1_attestation as ua


@pytest.fixture(autouse=True)
def _reset_state():
    def _clear():
        ua._token = None
        ua._expires_at = 0.0
        ua._last_failure = 0.0
        ua._refreshing = None
    _clear()
    yield
    _clear()


def test_fresh_cache_no_fetch(monkeypatch):
    calls = []

    async def spy(base_url):
        calls.append(base_url)

    monkeypatch.setattr(ua, "_do_fetch", spy)
    ua._token = "tok"
    ua._expires_at = time.time() + 3 * 86400  # 距到期 > 24h 边距 → 新鲜
    assert asyncio.run(ua.get_attestation("b")) == "tok"
    assert calls == []


def test_within_margin_returns_old_and_refreshes_in_background(monkeypatch):
    done = {"ok": False}

    async def fake(base_url):
        ua._token = "tok2"
        ua._expires_at = time.time() + 7 * 86400
        done["ok"] = True

    monkeypatch.setattr(ua, "_do_fetch", fake)
    ua._token = "old"
    ua._expires_at = time.time() + 3600  # 1h < 24h 边距 → 临期

    async def main():
        assert await ua.get_attestation("b") == "old"  # 本次不阻塞、交旧 token
        for _ in range(200):
            if done["ok"]:
                break
            await asyncio.sleep(0.005)
        assert done["ok"], "临期未触发后台刷新"

    asyncio.run(main())
    assert ua._token == "tok2"


def test_no_token_blocks_first_fetch(monkeypatch):
    async def slow(base_url):
        await asyncio.sleep(0.01)
        ua._token = "new"
        ua._expires_at = time.time() + 7 * 86400

    monkeypatch.setattr(ua, "_do_fetch", slow)
    assert asyncio.run(ua.get_attestation("b")) == "new"


def test_cooldown_after_failure_returns_none_without_refetch(monkeypatch):
    calls = []

    async def spy(base_url):
        calls.append(1)

    monkeypatch.setattr(ua, "_do_fetch", spy)
    ua._last_failure = time.time()
    assert asyncio.run(ua.get_attestation("b")) is None
    assert calls == []


def test_expired_token_treated_as_missing(monkeypatch):
    async def spy(base_url):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(ua, "_do_fetch", spy)
    ua._token = "dead"
    ua._expires_at = time.time() - 1
    ua._last_failure = time.time()  # 冷却中 → 直接 None，不阻塞重拉
    assert asyncio.run(ua.get_attestation("b")) is None
