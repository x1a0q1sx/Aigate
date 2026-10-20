"""连接池看门狗（2026-10-20）：池被长期占满时自愈。

背景（2026-10-19 生产事故）：一批并发流式请求（CodeBuddy Intl 全族，
单条 15–46s）把 20+10 的连接池占满后**再也没有归还**——15:48 起全站
DB 相关请求（含 /admin 仪表盘）持续 503 `database_pool_timeout`，
直到人工 `pm2 restart` 才恢复（宕了约 9 小时）。

单条连接池超时本身有护栏（503 短 JSON + 日志限流，见 exception_guard），
但护栏只保证「失败得体面」，不保证「服务恢复」。本看门狗补上后半段：
每 30s 采样 `engine.pool.checkedout()`，**持续满额 ≥120s** 判定为池故障
→ `engine.dispose()` 重建连接池：在途连接随归还作废，后续请求立即拿到
新连接，服务自愈、不再依赖人工重启。

判定刻意用「持续满额」而不是「瞬时满额」，避免正常并发峰值误触发；
一次性自愈后进入冷却（300s），抖动期不会反复 dispose。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_SAMPLE_SECONDS = 30.0      # 采样间隔
_SATURATED_SECONDS = 120.0  # 持续满额多久判定为故障（连续 4 次采样）
_COOLDOWN_SECONDS = 300.0   # 两次自愈之间的最小间隔

_task: Optional[asyncio.Task] = None


def decide_action(checked: int, capacity: int, saturated_since: Optional[float],
                  now: float, last_heal: float,
                  saturated_seconds: float = _SATURATED_SECONDS,
                  cooldown: float = _COOLDOWN_SECONDS) -> Tuple[str, Optional[float]]:
    """纯函数：由一次采样决定动作（便于测试）。

    返回 (action, new_saturated_since)，action ∈ {"idle", "observe", "heal"}。
    """
    if capacity <= 0 or checked < capacity:
        return "idle", None
    if saturated_since is None:
        return "observe", now
    if now - saturated_since >= saturated_seconds and now - last_heal >= cooldown:
        return "heal", None
    return "observe", saturated_since


def pool_state(engine) -> Tuple[int, int]:
    """(已签出连接数, 池容量=pool_size+max_overflow)；取不到时返回 (0, 0)。"""
    try:
        pool = engine.pool
        size = int(pool.size() or 0)
        overflow = int(getattr(pool, "_max_overflow", 0) or 0)
        return int(pool.checkedout()), size + overflow
    except Exception:
        return 0, 0


async def _loop() -> None:
    from server.db import engine

    logger.info("连接池看门狗已启动（每 %.0fs 采样；持续满额 %.0fs → 重建连接池）",
                _SAMPLE_SECONDS, _SATURATED_SECONDS)
    saturated_since: Optional[float] = None
    last_heal = 0.0
    while True:
        try:
            await asyncio.sleep(_SAMPLE_SECONDS)
            checked, capacity = pool_state(engine)
            now = time.monotonic()
            action, saturated_since = decide_action(checked, capacity, saturated_since, now, last_heal)
            if action == "observe" and saturated_since is not None:
                logger.warning("连接池满额观察中：checkedout=%d/%d", checked, capacity)
            elif action == "heal":
                logger.error("连接池持续满额超过 %.0fs（checkedout=%d/%d）→ 重建连接池自愈",
                             _SATURATED_SECONDS, checked, capacity)
                try:
                    await engine.dispose()
                    logger.error("连接池重建完成：后续请求使用新连接（旧连接随归还作废）")
                except Exception as e:  # pragma: no cover - 防御
                    logger.error("连接池重建失败：%s", e)
                last_heal = now
        except asyncio.CancelledError:
            logger.info("连接池看门狗已停止")
            break
        except Exception as e:  # pragma: no cover - 防御
            logger.warning("连接池看门狗异常：%s", e)


def start_pool_watchdog() -> Optional[asyncio.Task]:
    """启动看门狗（幂等；无事件循环时静默跳过）。"""
    global _task
    if _task is None or _task.done():
        try:
            _task = asyncio.create_task(_loop())
        except RuntimeError:
            return None
    return _task


async def stop_pool_watchdog() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    _task = None
