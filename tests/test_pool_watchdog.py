# -*- coding: utf-8 -*-
"""连接池看门狗（2026-10-20）——池被长期占满时自愈。

2026-10-19 生产事故：并发流式请求吃满 20+10 连接池且不归还，
15:48 起全站 DB 请求持续 503 `database_pool_timeout`，直到人工 pm2 restart。
看门狗负责后半段：持续满额 → engine.dispose() 重建连接池。
"""
from server.core.pool_watchdog import decide_action, pool_state


def test_decide_idle_when_below_capacity():
    action, since = decide_action(checked=3, capacity=30, saturated_since=None,
                                  now=100.0, last_heal=0.0)
    assert action == "idle" and since is None


def test_decide_observe_then_heal_after_threshold():
    # last_heal 远在过去，隔离「饱和时长」这一个判据
    # 首次满额：只观察，记住起点
    action, since = decide_action(checked=30, capacity=30, saturated_since=None,
                                  now=100.0, last_heal=-1000.0)
    assert action == "observe" and since == 100.0
    # 未到阈值（119s）：继续观察（起点不变）
    action, since = decide_action(checked=30, capacity=30, saturated_since=100.0,
                                  now=219.0, last_heal=-1000.0)
    assert action == "observe" and since == 100.0
    # 超过阈值（130s）：自愈
    action, since = decide_action(checked=30, capacity=30, saturated_since=100.0,
                                  now=230.0, last_heal=-1000.0)
    assert action == "heal" and since is None


def test_decide_respects_cooldown():
    # 刚自愈过（冷却期内）：即便持续满额也不再 dispose，避免抖动期反复重建
    action, since = decide_action(checked=30, capacity=30, saturated_since=100.0,
                                  now=230.0, last_heal=200.0)
    assert action == "observe" and since == 100.0


def test_decide_resets_when_pool_recovers():
    action, since = decide_action(checked=29, capacity=30, saturated_since=100.0,
                                  now=230.0, last_heal=0.0)
    assert action == "idle" and since is None


def test_pool_state_reads_engine_pool():
    class _Pool:
        def size(self):
            return 20

        def checkedout(self):
            return 7

        _max_overflow = 10

    class _Engine:
        pool = _Pool()

    assert pool_state(_Engine()) == (7, 30)


def test_pool_state_never_raises():
    class _Bad:
        @property
        def pool(self):
            raise RuntimeError("boom")

    assert pool_state(_Bad()) == (0, 0)
