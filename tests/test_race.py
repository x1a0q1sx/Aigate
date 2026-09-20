"""候选竞速 runner 回归测试。"""
import asyncio

import pytest

from server.core.race import NoMoreCandidates, RaceAllFailed, run_race


def run(coro):
    return asyncio.run(coro)


def test_first_fast_success():
    async def _t():
        started = []

        async def launch(i):
            started.append(i)
            return f"resp-{i}"

        idx, val = await run_race(launch, no_content_seconds=5)
        assert (idx, val) == (0, "resp-0")
        assert started == [0]
    run(_t())


def test_immediate_error_advances():
    async def _t():
        fails = []

        async def launch(i):
            if i == 0:
                raise RuntimeError("boom")
            return "ok"

        idx, val = await run_race(launch, 5, on_failure=lambda i, e, v: fails.append(i))
        assert (idx, val) == (1, "ok")
        assert fails == [0]
    run(_t())


def test_stalled_gets_overtaken_and_penalized():
    """0 号超时不出货 → 并行打 1 号；1 号先回 → 1 赢，0 被取消+罚时回调。"""
    async def _t():
        losers = []

        async def launch(i):
            if i == 0:
                await asyncio.sleep(10)  # 卡死（远超 race 时限）
                return "late"
            if i == 1:
                await asyncio.sleep(0.05)
                return "fast"
            raise NoMoreCandidates()

        idx, val = await run_race(launch, 0.15, on_loser=losers.append)
        assert (idx, val) == (1, "fast")
        assert losers == [0]
    run(_t())


def test_stalled_later_recovers_first_wins():
    """0 号 15s 没回、1 号已补发，但 0 号先醒 → 用 0 号，1 号罚时。"""
    async def _t():
        losers = []

        async def launch(i):
            if i == 0:
                await asyncio.sleep(0.30)  # 比 timeout 慢一点，但比 1 号快
                return "s0"
            if i == 1:
                await asyncio.sleep(10)
                return "s1"
            raise NoMoreCandidates()

        idx, val = await run_race(launch, 0.12, on_loser=losers.append)
        assert (idx, val) == (0, "s0")
        assert losers == [1]
    run(_t())


def test_classify_rejects_fake_success():
    async def _t():
        async def launch(i):
            return {"error": "empty stream"} if i == 0 else {"ok": 1}

        def classify(v):
            return "empty" if "error" in v else None

        idx, val = await run_race(launch, 5, classify=classify)
        assert idx == 1
    run(_t())


def test_exhausted_all_failed_raises():
    async def _t():
        async def launch(i):
            if i < 2:
                raise RuntimeError(f"e{i}")
            raise NoMoreCandidates()

        with pytest.raises(RaceAllFailed) as ei:
            await run_race(launch, 5)
        assert [e[0] for e in ei.value.errors] == [0, 1]
    run(_t())


def test_launch_cancellation_cleanup_runs():
    """被取消的 launch 协程 finally 必须执行（生成器 aclose 等清理）。"""
    async def _t():
        cleaned = []

        async def launch(i):
            try:
                if i == 0:
                    await asyncio.sleep(10)
                    return "never"
                await asyncio.sleep(0.02)
                return "win"
            finally:
                if i == 0:
                    cleaned.append(0)

        idx, val = await run_race(launch, 0.1)
        assert (idx, val) == (1, "win")
        assert cleaned == [0]
    run(_t())
