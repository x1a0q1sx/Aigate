# -*- coding: utf-8 -*-
"""共享测试夹具（2026-10-16）。"""
import pytest


@pytest.fixture(autouse=True)
def _reset_oauth_balancer():
    """account_balancer 是进程全局态：滑窗计数会跨测试累积（同一进程 60s 内
    累计 pick 数达到 enter_requests 就把分流打开），逐测试重置保证确定性。"""
    from server.core import account_balancer
    account_balancer.reset()
    yield
    account_balancer.reset()
