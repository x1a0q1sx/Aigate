# -*- coding: utf-8 -*-
"""共享测试夹具（2026-10-16）。"""
import pytest


@pytest.fixture(autouse=True)
def _reset_oauth_balancer():
    """account_balancer 是进程全局态：滑窗计数会跨测试累积（同一进程 60s 内
    累计 pick 数达到 enter_requests 就把分流打开），逐测试重置保证确定性。
    同理由重置 _ACCOUNT_DIRTY（账号脏标记，跨测试残留会让成功路径意外查库）
    与 _DEVICE_FLOW_STATUS（设备流终态，跨测试残留会让 device-status 用例互扰）。"""
    from server.core import account_balancer
    from server.core import oauth_client as _oc
    account_balancer.reset()
    _oc._ACCOUNT_DIRTY.clear()
    _oc._DEVICE_FLOW_STATUS.clear()
    yield
    account_balancer.reset()
    _oc._ACCOUNT_DIRTY.clear()
    _oc._DEVICE_FLOW_STATUS.clear()
