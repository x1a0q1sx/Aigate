"""候选竞速（race）：组合/auto 回退链的"15 秒无内容就并行打下一家"。

语义（用户口径）：
  - 当前候选在 no_content_seconds 内没有任何返回 → 不等它，自动再打下一候选；
  - 谁先返回结果就用谁的（被超前的候选仍在后台跑，若它先回来也算它赢）；
  - 落败（被取消）的候选自动罚时——由调用方在 on_loser 里 mark_cooling。

与旧的"顺序回退 + 首块超时取消"的区别：旧实现超时就**杀掉**当前候选；
本模块不杀，两个在途谁先出结果用谁，把"慢但没死"的候选的产出也保住。

launch(i) 约定：
  - 协程执行第 i 个候选的完整尝试（预检→凭证→发请求→拿到"首个可用结果"），
    成功返回 value（流式=已缓冲到实质块的生成器上下文；非流式=响应 dict）；
  - 任何异常 = 本次尝试失败（含 NoMoreCandidates=候选耗尽，不计失败）；
  - 协程必须自己处理取消清理（finally aclose 等）——被取消时调用方视为"被超时的失败"。
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


class NoMoreCandidates(Exception):
    """launch 内部抛出：没有第 i 个候选了（候选耗尽 / auto 选不出新模型）。"""


class RaceAllFailed(Exception):
    def __init__(self, errors):
        self.errors = errors or []
        super().__init__("race: all attempts failed: %s" %
                         (self.errors[-3:] if self.errors else []))


async def run_race(launch, no_content_seconds, classify=None,
                   on_failure=None, on_loser=None, max_inflight=2):
    """返回 (winner_index, value)。全部失败/耗尽 → RaceAllFailed。

    classify(value) -> None(成功) | "错误文本"(伪成功判失败)。
    on_failure(i, err_text, value_or_none)：一次失败尝试（记 attempt 日志用）。
    on_loser(i)：胜出者已定，被取消的在途候选（罚时用）。
    """
    i = 0
    exhausted = False
    outstanding = {}  # task -> attempt_index
    errs = []

    def _start(idx):
        nonlocal exhausted
        coro = launch(idx)
        t = asyncio.create_task(coro)
        outstanding[t] = idx
        return t

    while True:
        timeout = None
        if not outstanding:
            if exhausted:
                break
            _start(i); i += 1
            timeout = no_content_seconds
        elif len(outstanding) < max_inflight and not exhausted:
            timeout = no_content_seconds

        done, _ = await asyncio.wait(list(outstanding.keys()),
                                     timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            # 时限到仍无一在途返回 → 有候选就并行补一发（不杀现有在途）
            if len(outstanding) < max_inflight and not exhausted:
                _start(i); i += 1
            continue

        decided = None
        for t in done:
            idx = outstanding.pop(t)
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                if isinstance(exc, NoMoreCandidates):
                    exhausted = True
                else:
                    et = f"{type(exc).__name__}: {str(exc)[:300]}"
                    errs.append((idx, et))
                    if on_failure:
                        try:
                            r = on_failure(idx, et, exc)
                            if asyncio.iscoroutine(r):
                                await r
                        except Exception:
                            pass
                continue
            value = t.result()
            err = classify(value) if classify else None
            if err:
                errs.append((idx, err))
                if on_failure:
                    try:
                        r = on_failure(idx, err, value)
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:
                        pass
                continue
            decided = (idx, value)
            break

        if decided:
            # 其余在途全部取消 → 交调用方罚时
            cancelled = []
            for t2 in list(outstanding.keys()):
                idx2 = outstanding.pop(t2)
                t2.cancel()
                cancelled.append(t2)
                if on_loser:
                    try:
                        r = on_loser(idx2)
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:
                        pass
            # 让取消传播（launch 协程的 finally 清理跑完）
            if cancelled:
                await asyncio.gather(*cancelled, return_exceptions=True)
            return decided

        if not outstanding and exhausted:
            break

    # 收尾：全部失败
    if outstanding:
        for t in list(outstanding.keys()):
            t.cancel()
        await asyncio.gather(*outstanding.keys(), return_exceptions=True)
    raise RaceAllFailed(errs)
