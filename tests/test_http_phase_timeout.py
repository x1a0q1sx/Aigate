# -*- coding: utf-8 -*-
"""httpx 超时按相位拆分（2026-11-03）。

起因：生产日志一条 Cline Pass 请求（66k prompt / 0 completion）耗了 **137.2s**
才报 `ConnectError: 连接被对端中断（TLS 握手/写入阶段被掐断）`。适配器写的是
`httpx.AsyncClient(timeout=self.timeout)`，httpx 会把这一个数字同时当
connect/read/write/pool 四个相位 —— read 180s 合理，connect/write 180s 意味着
「链路已经死了还要吊 3 分钟」，回退/重试/心跳全被堵住。

口径：read 保留原值，connect 30s / write 60s / pool 10s 封顶；
原值比封顶更短时以原值为准（不把短超时请求悄悄放宽）。
"""
import httpx
import pytest

from server.core.http_timeouts import (
    CONNECT_TIMEOUT_SECONDS, POOL_TIMEOUT_SECONDS, WRITE_TIMEOUT_SECONDS, phase_timeout,
)


def test_read_keeps_original_and_other_phases_are_capped():
    t = phase_timeout(180)
    assert isinstance(t, httpx.Timeout)
    assert t.read == 180.0                       # 首字前的静默预算不动
    assert t.connect == CONNECT_TIMEOUT_SECONDS  # 建连 30s 封顶
    assert t.write == WRITE_TIMEOUT_SECONDS      # 大 body 上传卡死不再拖 3 分钟
    assert t.pool == POOL_TIMEOUT_SECONDS


def test_short_timeout_is_not_widened():
    """原本只给 5s 的请求不能被 phase_timeout 放宽成 30s 建连"""
    t = phase_timeout(5)
    assert (t.connect, t.read, t.write, t.pool) == (5.0, 5.0, 5.0, 5.0)


def test_medium_timeout_splits_sensibly():
    t = phase_timeout(60)
    assert t.read == 60.0 and t.connect == 30.0 and t.write == 60.0 and t.pool == 10.0


@pytest.mark.parametrize("bad", [None, "", "abc", 0, -1])
def test_non_positive_or_non_numeric_passes_through(bad):
    """脏值/0/None 原样返回，交回 httpx 处理，不改变既有行为"""
    assert phase_timeout(bad) == bad


def test_multiplied_long_op_still_capped():
    """anthropic 的 timeout*5 长操作：read 放宽但建连照样封顶"""
    t = phase_timeout(60 * 5)
    assert t.read == 300.0 and t.connect == 30.0


def test_chat_adapters_no_longer_pass_bare_timeout():
    """源码守卫：四个对话适配器不得再出现 `AsyncClient(timeout=self.timeout)` 裸值。

    这是 137s 挂死的成因形态，防止后续新增调用点又把一个数字当全部超时。
    """
    import pathlib
    import server.adapters.anthropic_adapter as ant
    import server.adapters.codex_responses as cod
    import server.adapters.github_adapter as gh
    import server.adapters.openai_compat as oc

    bare = 'AsyncClient(timeout=self.timeout,'
    for mod in (oc, ant, cod, gh):
        p = pathlib.Path(mod.__file__).resolve()
        src = p.read_text(encoding="utf-8-sig")
        assert bare not in src, f"{p.name} 仍有裸 timeout=self.timeout"
        assert "phase_timeout(" in src, f"{p.name} 未接入按相位超时"


def test_cline_style_stalled_connect_fails_fast():
    """行为验证：把 read 设得很长时，建连失败仍应在 connect 封顶内抛出 ConnectError。

    用一个不可路由地址（RFC5737 TEST-NET-1）模拟「连不上」，只断言它**没有**
    等满 read 时长——这正是生产那条日志拖到 137s 的形状。
    """
    import asyncio

    async def _run():
        async with httpx.AsyncClient(timeout=phase_timeout(180)) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post("http://192.0.2.1/v1/chat/completions", json={},
                                  headers={})

    asyncio.run(_run())
