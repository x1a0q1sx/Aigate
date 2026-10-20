# -*- coding: utf-8 -*-
"""前端静态回归：设置页「失败自动重试」开关（2026-10-20）。

用户需求：「设置里加一个功能开关，开启后自动重试，对一个模型请求出错后，自动重试
一次，先不返回报错，如果两次都报错，再返回报错。」后端行为见
tests/test_adapter_retry.py；这里只锁前端接线不失效（无 JS 测试运行时，
按仓库既有约定做源码静态扫描）。
"""
from pathlib import Path

_CLIENT = Path("client/src")


def _read(rel: str) -> str:
    return (_CLIENT / rel).read_text(encoding="utf-8")


def test_settings_renders_retry_card():
    src = _read("views/Settings.vue")
    assert "失败自动重试" in src
    assert "retry.enabled" in src
    assert "saveRetry" in src and "loadRetry" in src
    # 卡片说明必须写明边界，避免用户误以为「任何错误都会重试」
    assert "流式" in src
    assert "两次都失败才返回错误" in src


def test_settings_load_includes_retry():
    src = _read("views/Settings.vue")
    assert "this.loadRetry()" in src
    # 默认态：关闭（关闭后保持现状不动）
    assert "retry: { enabled: false" in src


def test_api_exposes_retry_endpoints():
    src = _read("api.js")
    assert "getRetry: () => apiGet('/admin/api/retry')" in src
    assert "updateRetry: (data) => apiPut('/admin/api/retry', data)" in src


def test_settings_does_not_invent_endpoints():
    src = _read("views/Settings.vue")
    assert "/admin/api/retry" not in src   # 只能通过 api.js 调用
