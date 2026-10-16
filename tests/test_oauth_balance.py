# -*- coding: utf-8 -*-
"""OAuth 多账号频率感知分流（2026-10-16 用户要求）。

需求：请求频率高时自动多账号负载，频率低时保持 F40 的「锁定最优账号」模式。
- 滑窗计数达 enter_requests → 分流；低于 exit_requests → 退回粘住（滞回）
- 分流只在**同额度档位**内轮流：冷却账号、已耗尽账号绝不为分流而吃到流量
- 点名（provider.oauth_owner）不受影响；单账号 provider 自然无操作
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.models.oauth_token import OAuthToken


class _FakeCrypto:
    def encrypt(self, s): return "enc-" + s
    def decrypt(self, s): return (s[4:] if s.startswith("enc-") else s)


async def _db(tmp_path, tables):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'b.db'}")
    async with engine.begin() as conn:
        for t in tables:
            await conn.run_sync(t.create)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _setup(tmp_path, owners=("acct-a", "acct-b")):
    engine, Session = await _db(tmp_path, [OAuthToken.__table__])
    async with Session() as db:
        for o in owners:
            db.add(OAuthToken(provider_code="codebuddy_cn", owner=o,
                              access_token_enc=f"enc-tok-{o}", is_active=True))
        await db.commit()
    return engine, Session


def _q(remaining, expires=None):
    q = {"used": 100 - remaining, "total": 100, "remaining": remaining, "unlimited": False}
    if expires:
        q["reset_at"] = expires
    return q


def _usage_cache(a_exp_days=1, b_exp_days=30):
    """两个账号都有余额、到期时间不同（a 更早到期 → 粘住模式应永远选 a）。"""
    import server.core.oauth_usage as ou
    now = datetime.utcnow()
    return ou, {
        ou._key("codebuddy_cn", "tok-acct-a"): (ou.time.monotonic() + 999,
            {"quotas": {"p": _q(50, (now + timedelta(days=a_exp_days)).isoformat())}}),
        ou._key("codebuddy_cn", "tok-acct-b"): (ou.time.monotonic() + 999,
            {"quotas": {"p": _q(50, (now + timedelta(days=b_exp_days)).isoformat())}}),
    }


def _fake_cfg(enabled=True, window=60, enter=10, exit_=3):
    return SimpleNamespace(oauth_balance=SimpleNamespace(
        enabled=enabled, window_seconds=window, enter_requests=enter, exit_requests=exit_))


# ── 1) 分流器本体（纯函数/状态机） ─────────────────────────────

def test_balancer_enters_and_exits_with_hysteresis(monkeypatch):
    """滑窗计数达 enter 进入分流；随时间滑出窗口、降到 exit 以下退回粘住。"""
    from server.core import account_balancer as ab
    monkeypatch.setattr("server.config.get_config", lambda: _fake_cfg(enter=5, exit_=2))
    t = 1000.0
    for i in range(4):
        ab.note_selection("p", "a", now=t + i)
    assert ab.is_balancing("p", now=t + 10) is False   # 4 < 5：未达阈值
    ab.note_selection("p", "a", now=t + 4)
    assert ab.is_balancing("p", now=t + 10) is True    # 5 ≥ 5：进入分流
    # 窗口滑过（t+80 时，前 5 笔全部落在 60s 窗口外）
    assert ab.is_balancing("p", now=t + 80) is False
    # 滞回：达 enter 进入后，只要不低于 exit 就保持
    for i in range(4):
        ab.note_selection("q", "a", now=t + i)
    assert ab.is_balancing("q", now=t + 10) is False
    ab.note_selection("q", "a", now=t + 4)
    ab.note_selection("q", "a", now=t + 5)
    assert ab.is_balancing("q", now=t + 10) is True
    # 只剩 3 笔（≥exit=2）仍在分流
    ab.note_selection("q", "b", now=t + 6)
    assert ab.is_balancing("q", now=t + 10) is True


def test_balancer_disabled_is_inert(monkeypatch):
    from server.core import account_balancer as ab
    monkeypatch.setattr("server.config.get_config", lambda: _fake_cfg(enabled=False))
    for i in range(50):
        ab.note_selection("p", "a", now=1000.0 + i)
    assert ab.is_balancing("p", now=2000.0) is False
    assert ab.stats(now=2000.0) == {} or ab.stats(now=2000.0).get("p", {}).get("balancing") is False


def test_balancer_load_key_prefers_least_used(monkeypatch):
    from server.core import account_balancer as ab
    monkeypatch.setattr("server.config.get_config", lambda: _fake_cfg())
    t = 1000.0
    ab.note_selection("p", "a", now=t)
    ab.note_selection("p", "a", now=t + 1)
    ab.note_selection("p", "b", now=t + 2)
    ka = ab.account_load_key("p", "a", now=t + 3)
    kb = ab.account_load_key("p", "b", now=t + 3)
    assert ka[0] == 2 and kb[0] == 1
    assert kb < ka                                  # b 更少 → 优先
    # 同计数时「最近使用更早」的优先（轮流）
    ab.note_selection("p", "b", now=t + 4)
    assert ab.account_load_key("p", "a", now=t + 5)[1] < ab.account_load_key("p", "b", now=t + 5)[1]


# ── 2) 低频：保持 F40 粘住语义（回归守卫） ─────────────────────

@pytest.mark.asyncio
async def test_low_frequency_stays_sticky(tmp_path, monkeypatch):
    """低频（未达阈值）→ 永远选「最早到期」那个账号，与 F40 完全一致。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    ou, cache = _usage_cache()
    monkeypatch.setattr(ou, "_CACHE", cache)
    try:
        async with Session() as db:
            picks = [await client.pick_access_token_ex("codebuddy_cn", db) for _ in range(3)]
        assert [p[0] for p in picks] == ["acct-a"] * 3   # a 更早到期 → 一直用它
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_high_frequency_underused_account_catches_up(tmp_path, monkeypatch):
    """高频进入分流后：窗口内请求数少的账号先接管流量（负载追赶）。

    粘住期 a 吃掉全部流量 → 进入分流后计数为 0 的 b 先接管，直到追平。"""
    from server.core.oauth_client import get_oauth_client
    from server.core import account_balancer as ab
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    ou, cache = _usage_cache()
    monkeypatch.setattr(ou, "_CACHE", cache)
    try:
        async with Session() as db:
            # 连续取号推过阈值（enter=10；粘住期全打 a）
            for _ in range(12):
                await client.pick_access_token_ex("codebuddy_cn", db)
            assert ab.is_balancing("codebuddy_cn") is True
            # 计数为 0 的 b 先接管（a 的窗口计数仍在，要等它追平）
            seq = [(await client.pick_access_token_ex("codebuddy_cn", db))[0] for _ in range(2)]
            assert seq == ["acct-b", "acct-b"], f"追赶未生效：{seq}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_high_frequency_alternates_once_balanced(tmp_path, monkeypatch):
    """窗口内计数均衡后：同档位（都有余额）账号严格轮流。"""
    from server.core.oauth_client import get_oauth_client
    from server.core import account_balancer as ab
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    ou, cache = _usage_cache()
    monkeypatch.setattr(ou, "_CACHE", cache)
    try:
        # 交替记账把两账号窗口计数推到均衡且过阈值（enter=10 → 各 6 笔共 12）
        for _ in range(6):
            ab.note_selection("codebuddy_cn", "acct-a")
            ab.note_selection("codebuddy_cn", "acct-b")
        assert ab.is_balancing("codebuddy_cn") is True
        async with Session() as db:
            seq = [(await client.pick_access_token_ex("codebuddy_cn", db))[0] for _ in range(6)]
        assert len(set(seq)) == 2, f"分流未生效：{seq}"
        assert all(seq[i] != seq[i + 1] for i in range(len(seq) - 1)), f"未轮流：{seq}"
    finally:
        await engine.dispose()


# ── 3) 分流不得越过额度/冷却闸门 ───────────────────────────────

@pytest.mark.asyncio
async def test_cooling_account_not_chosen_even_when_least_loaded(tmp_path, monkeypatch):
    """分流模式下冷却账号仍被跳过（哪怕它计数最少）。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    ou, cache = _usage_cache(b_exp_days=1, a_exp_days=30)   # b 更早到期（本来优先）
    monkeypatch.setattr(ou, "_CACHE", cache)
    try:
        async with Session() as db:
            # 高频预热：让 b 冷却
            for _ in range(12):
                await client.pick_access_token_ex("codebuddy_cn", db)
            row = (await db.execute(select(OAuthToken).where(OAuthToken.owner == "acct-b"))).scalar_one()
            row.cooldown_until = datetime.utcnow() + timedelta(minutes=5)
            await db.commit()
            got = await client.pick_access_token_ex("codebuddy_cn", db)
        assert got[0] == "acct-a"      # 冷却的 b 不因分流被选中
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_drained_account_not_balanced_into(tmp_path, monkeypatch):
    """分流模式下「已耗尽」账号（档位 2）不会被分流喂流量。"""
    import server.core.oauth_usage as ou
    from server.core.oauth_client import get_oauth_client
    from server.core import account_balancer as ab
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    now = datetime.utcnow()
    monkeypatch.setattr(ou, "_CACHE", {
        ou._key("codebuddy_cn", "tok-acct-a"): (ou.time.monotonic() + 999,
            {"quotas": {"p": _q(0, (now + timedelta(days=1)).isoformat())}}),      # 耗尽
        ou._key("codebuddy_cn", "tok-acct-b"): (ou.time.monotonic() + 999,
            {"quotas": {"p": _q(50, (now + timedelta(days=30)).isoformat())}}),    # 有余额
    })
    try:
        # 直接置分流态（阈值推到很低），避免依赖预热次数
        monkeypatch.setattr("server.config.get_config", lambda: SimpleNamespace(
            oauth_balance=SimpleNamespace(enabled=True, window_seconds=60,
                                          enter_requests=2, exit_requests=1)))
        ab.note_selection("codebuddy_cn", "acct-b")
        ab.note_selection("codebuddy_cn", "acct-b")
        assert ab.is_balancing("codebuddy_cn") is True
        async with Session() as db:
            got = await client.pick_access_token_ex("codebuddy_cn", db)
        assert got[0] == "acct-b"      # a 已耗尽 → 哪怕分流也不喂给它
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_owner_unaffected_by_balancing(tmp_path, monkeypatch):
    """点名取号不受分流影响（用户指定就照用）。"""
    from server.core.oauth_client import get_oauth_client
    from server.core import account_balancer as ab
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        for i in range(20):
            ab.note_selection("codebuddy_cn", "acct-a")
        assert ab.is_balancing("codebuddy_cn") is True
        async with Session() as db:
            got = await client.pick_access_token_ex("codebuddy_cn", db, owner="acct-b")
        assert got == ("acct-b", "tok-acct-b")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_single_account_is_noop(tmp_path, monkeypatch):
    """单账号 provider：高频也不产生任何分流行为。"""
    from server.core.oauth_client import get_oauth_client
    from server.core import account_balancer as ab
    engine, Session = await _setup(tmp_path, owners=("only",))
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    try:
        async with Session() as db:
            for _ in range(15):
                got = await client.pick_access_token_ex("codebuddy_cn", db)
                assert got[0] == "only"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_config_keeps_f40_behaviour(tmp_path, monkeypatch):
    """enabled=False → 高频也保持粘住（完全回退 F40）。"""
    from server.core.oauth_client import get_oauth_client
    engine, Session = await _setup(tmp_path)
    client = get_oauth_client()
    monkeypatch.setattr(client, "_crypto", _FakeCrypto())
    ou, cache = _usage_cache()
    monkeypatch.setattr(ou, "_CACHE", cache)
    monkeypatch.setattr("server.config.get_config", lambda: _fake_cfg(enabled=False))
    try:
        async with Session() as db:
            owners = [(await client.pick_access_token_ex("codebuddy_cn", db))[0] for _ in range(15)]
        assert set(owners) == {"acct-a"}     # a 更早到期 → 始终粘住
    finally:
        await engine.dispose()


# ── 4) 接线守卫 ────────────────────────────────────────────────

def test_rank_key_shape_and_no_regression_when_idle():
    """低频路径的排序键必须与 F40 等价：冷却 > 额度档位 > 到期 > 剩余 > __default > id。"""
    import inspect
    from server.core import oauth_client as oc
    src = inspect.getsource(oc.OAuthClient._rank_auto_rows)
    assert "account_balancer" in src
    assert "is_balancing" in src
    assert "account_load_key" in src
    # 低频时用恒定负载位，保证与 F40 排序完全一致
    assert "(0, 0.0)" in src


def test_selection_is_recorded_on_both_paths():
    """取号成功必须记账（自动 + 点名两条路径），否则分流永不触发。"""
    import inspect
    from server.core import oauth_client as oc
    src = inspect.getsource(oc.OAuthClient.pick_access_token_ex)
    assert src.count("note_selection") == 2


def test_config_defaults_present():
    from server.config import OAuthBalanceConfig
    c = OAuthBalanceConfig()
    assert c.enabled is True
    assert c.window_seconds == 60
    assert c.enter_requests == 10
    assert c.exit_requests == 3
    assert c.exit_requests < c.enter_requests     # 滞回必须成立
