"""异步日志写入队列（P0-3 SQLite 写入队列化）。

问题：请求路径上每条日志独立 open-session → dedup(blob) → add → commit，
高频请求下与限流/路由决策/归档互相争 SQLite 写锁（database is locked），
且日志 commit 时间直接计入请求延迟。

方案：请求路径 enqueue_log() 立即返回（零 DB 写入）；后台 worker 批量取日志、
逐条去重、单事务 commit（50 条 / 500ms 阈值）。队列满时丢弃并计数——
保护网关主流程优先。服务关闭时 flush 剩余队列。

附带 WAL 周期性 checkpoint（30 分钟，TRUNCATE 收缩 WAL 文件）。
"""
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_QUEUE_MAX = 1000
_BATCH_MAX = 50
_BATCH_WAIT = 0.5
_CHECKPOINT_INTERVAL = 1800  # 30 分钟

_queue: Optional[asyncio.Queue] = None
_worker_task: Optional[asyncio.Task] = None
_checkpoint_task: Optional[asyncio.Task] = None
_last_good_route_note = ""
stopped = False

stats = {
    "enqueued": 0,
    "written": 0,
    "dropped": 0,
    "errors": 0,
    "batches": 0,
    "last_error": "",
    "started_at": "",
}


def _get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    return _queue


async def enqueue_log(**kwargs) -> bool:
    """请求路径调用：入队立即返回。True=已入队（异步落库），False=队列满被丢弃。"""
    if stopped:
        return False
    q = _get_queue()
    try:
        q.put_nowait(kwargs)
        stats["enqueued"] += 1
        return True
    except asyncio.QueueFull:
        stats["dropped"] += 1
        stats["last_error"] = "log queue full"
        return False


def is_running() -> bool:
    """队列是否处于运行状态（write_log 据此选择入队或同步写）。"""
    return not stopped and _worker_task is not None and not _worker_task.done()


async def _write_batch(batch: list) -> None:
    """一批日志（≤50 条）单事务落库；整批失败降级为逐条重试一次，仍失败丢弃计数。"""
    from server.db import AsyncSessionLocal
    from server.models.request_log import RequestLog
    from server.core.request_logger import dedup_log_row

    written = 0
    async with AsyncSessionLocal() as db:
        recs = []
        for kwargs in batch:
            try:
                rec = RequestLog(**kwargs)
                await dedup_log_row(db, rec)
                db.add(rec)
                recs.append(rec)
            except Exception as e:
                stats["dropped"] += 1
                stats["last_error"] = f"dedup: {str(e)[:150]}"
        try:
            await db.commit()
            written = len(recs)
        except Exception as e:
            await db.rollback()
            stats["last_error"] = f"batch commit: {str(e)[:150]}"
            for kwargs in batch:  # 整批失败 → 逐条重试一次
                try:
                    rec = RequestLog(**kwargs)
                    await dedup_log_row(db, rec)
                    db.add(rec)
                    await db.commit()
                    written += 1
                except Exception as e2:
                    stats["dropped"] += 1
                    stats["last_error"] = f"retry: {str(e2)[:150]}"
                    try:
                        await db.rollback()
                    except Exception:
                        pass
    stats["written"] += written
    stats["batches"] += 1


async def _worker():
    q = _get_queue()
    while not stopped:
        try:
            first = await asyncio.wait_for(q.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        batch = [first]
        deadline = time.time() + _BATCH_WAIT
        while len(batch) < _BATCH_MAX and time.time() < deadline:
            try:
                batch.append(q.get_nowait())
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.02)
        try:
            await _write_batch(batch)
        except Exception as e:
            stats["errors"] += 1
            stats["last_error"] = str(e)[:200]
            logger.warning("log batch write failed: %s", e)


async def _wal_checkpoint_loop():
    while not stopped:
        await asyncio.sleep(_CHECKPOINT_INTERVAL)
        try:
            from server.db import engine
            from sqlalchemy import text
            async with engine.begin() as conn:
                await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        except Exception as e:
            logger.debug("wal checkpoint skipped: %s", e)


def start_log_queue() -> None:
    global _worker_task, _checkpoint_task, stopped
    stopped = False
    stats["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())
        _checkpoint_task = asyncio.create_task(_wal_checkpoint_loop())
        logger.info("Log write queue started (queue_max=%d, batch=%d/%.1fs)", _QUEUE_MAX, _BATCH_MAX, _BATCH_WAIT)


async def stop_log_queue() -> None:
    """停止 worker 并 flush 剩余队列（服务关闭时调用）。"""
    global stopped, _worker_task
    stopped = True
    if _worker_task is not None and not _worker_task.done():
        # worker 在 stopped=True 后会把队列剩余部分取完（get 超时循环退出前再清一轮）
        q = _get_queue()
        deadline = time.time() + 8
        while not q.empty() and time.time() < deadline:
            await asyncio.sleep(0.1)
        try:
            await asyncio.wait_for(_worker_task, timeout=5)
        except Exception:
            pass
    _worker_task = None
    logger.info("Log write queue stopped: %s", stats)
