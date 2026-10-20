"""上游调用失败自动重试（设置页「失败自动重试」开关）。

需求（2026-10-20）：设置里加一个功能开关，开启后对一个模型请求出错时
**先不返回报错**，自动原样重试一次；两次都失败才返回错误。关闭时保持现状。

实现位置：所有推理入口最终都会经由 `model_catalog.create_adapter_for_provider`
（标准 / OAuth / atomcode 渠道）或 `free_providers.get_free_executor`
（free_tier 渠道）拿到调用器，因此在这里做一层代理包装即可一次性覆盖
/v1 直连、auto、combo、playground、fusion、Anthropic/Responses/Gemini 转译入口。

语义与边界（刻意保守，避免把确定性失败也重试一遍）：

- 非流式：失败 → 等待 `delay_seconds` → 原样重试一次；两次都失败才抛出。
- 流式：只有**尚未向客户端吐出任何实质内容**（正文/思考/工具调用）时才重试。
  已经输出内容后再重试会造成重复输出，因此直接抛出。元数据 chunk（role-only /
  usage-only）会先缓冲，重试时丢弃，不会泄漏给客户端。
- 不重试：`CancelledError`、上下文超限、额度/余额耗尽、每日限额、4xx
  （408/425/429 除外）——这些重试也必然失败。
- 与既有「同 key 瞬态重试」不叠加：自动重试用尽后异常带
  `_aigate_retry_exhausted=True`，`v1_router._retry_budget_for` 会返回 0，
  避免一次请求被重试 3~6 次。
- 适配器内部的 `chat_completion → self.stream_chat_completion` 自调用不会被
  二次包装（代理只包外层对象，内部 `self` 仍是原始适配器）。

默认关闭：配置缺失 / 读取失败一律视为关闭，行为与改动前完全一致。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# 运行统计（GET /admin/api/retry 返回，便于生产验证开关确实生效）
_STATS = {
    "wrapped": 0,       # 被包装的调用器数量（开启后每请求 +1）
    "retried": 0,       # 实际发起的重试次数
    "recovered": 0,     # 重试后成功的次数
    "exhausted": 0,     # 两次都失败、错误最终返回给客户端的次数
    "skipped": 0,       # 判定为不可重试（4xx/上下文/额度等）的次数
}


def get_retry_stats() -> dict:
    return dict(_STATS)


def reset_retry_stats() -> None:
    for k in _STATS:
        _STATS[k] = 0


class UpstreamResultError(RuntimeError):
    """适配器以返回值形态报告的上游错误（{"error": ...} 且无 choices）。"""


def retry_settings() -> tuple[bool, float]:
    """读取配置：(enabled, delay_seconds)。任何异常都回退为关闭。"""
    try:
        from server.config import get_config
        cfg = getattr(get_config(), "retry", None)
        enabled = bool(getattr(cfg, "enabled", False))
        try:
            delay = float(getattr(cfg, "delay_seconds", 1.0) or 0.0)
        except (TypeError, ValueError):
            delay = 1.0
        return enabled, max(0.0, min(30.0, delay))
    except Exception:
        return False, 1.0


def _status_code(e: BaseException) -> Optional[int]:
    st = getattr(e, "status_code", None)
    if st is None:
        st = getattr(getattr(e, "response", None), "status_code", None)
    try:
        return int(st) if st is not None else None
    except (TypeError, ValueError):
        return None


def is_retryable_error(e: BaseException) -> bool:
    """该异常是否值得原样重试一次（确定性失败不重试）。"""
    if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return False
    try:
        from server.core.error_text import err_text
        text = err_text(e)
    except Exception:
        text = f"{type(e).__name__}: {e}"
    try:
        from server.core.context_guard import is_context_error
        if is_context_error(text):
            return False
    except Exception:
        pass
    try:
        from server.core.quota_limit import is_quota_related_error
        if is_quota_related_error(text):
            return False
    except Exception:
        pass
    st = _status_code(e)
    if st is not None:
        if st in (408, 425, 429):
            return True
        if 400 <= st < 500:
            return False          # 400/401/402/403/404/409/413/422… 重试必然再失败
        if st >= 500:
            return True
    return True


def _mark_exhausted(e: BaseException) -> None:
    try:
        setattr(e, "_aigate_retry_exhausted", True)
    except Exception:
        pass


def _is_error_result(result: Any) -> bool:
    return (isinstance(result, dict) and bool(result.get("error"))
            and not result.get("choices"))


def _is_error_chunk(ck: Any) -> bool:
    return isinstance(ck, dict) and bool(ck.get("error")) and not ck.get("choices")


def _error_text(payload: Any) -> str:
    try:
        err = payload.get("error") if isinstance(payload, dict) else payload
        return str(err)[:500]
    except Exception:
        return "unknown upstream error"


def _chunk_has_substance(ck: Any) -> bool:
    """chunk 是否携带实质输出：正文 / 思考内容 / 工具调用任一非空。

    与 v1_router._chunk_has_substance 保持同一口径；此处不 import v1_router
    以免形成循环依赖。"""
    if not isinstance(ck, dict):
        return False
    for ch in (ck.get("choices") or []):
        if not isinstance(ch, dict):
            continue
        d = ch.get("delta") or {}
        if isinstance(d.get("content"), str) and d.get("content"):
            return True
        rc = d.get("reasoning_content")
        if isinstance(rc, str) and rc:
            return True
        if d.get("tool_calls"):
            return True
        m = ch.get("message")
        if isinstance(m, dict) and (m.get("content") or m.get("tool_calls")):
            return True
    return False


async def _sleep_before_retry(delay: float) -> None:
    if delay > 0:
        await asyncio.sleep(delay)


async def _call_with_retry(fn: Callable[[], Any], *, delay: float, label: str) -> Any:
    """非流式：失败后原样重试一次；两次都失败才把错误交回上层。

    - 第二次失败的异常带 `_aigate_retry_exhausted` 标记（防止外层再叠加重试）；
    - 适配器以返回值形态报错（{"error": ...} 无 choices）同样算失败，但第二次
      仍失败时**原样返回该错误体**，保持改动前的响应形态。"""
    for attempt in range(2):
        try:
            result = await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if attempt >= 1:
                _mark_exhausted(e)
                _STATS["exhausted"] += 1
                raise
            if not is_retryable_error(e):
                _STATS["skipped"] += 1
                raise
            _STATS["retried"] += 1
            logger.warning("上游调用失败，自动重试一次（%s）：%s: %s",
                           label, type(e).__name__, str(e)[:300])
            await _sleep_before_retry(delay)
            continue
        if not _is_error_result(result):
            if attempt > 0:
                _STATS["recovered"] += 1
            return result
        # 返回值形态的错误
        if attempt >= 1:
            _STATS["exhausted"] += 1
            return result
        _STATS["retried"] += 1
        logger.warning("上游调用返回错误体，自动重试一次（%s）：%s",
                       label, _error_text(result)[:300])
        await _sleep_before_retry(delay)
    raise RuntimeError("retry loop exhausted")  # pragma: no cover


async def _stream_with_retry(make_gen: Callable[[], Any], *, delay: float, label: str):
    """流式：实质内容出现前失败才重试；已输出内容后失败直接抛出。

    实质内容出现前的元数据 chunk 先缓冲；重试时丢弃上一轮缓冲，客户端无感。
    重试也用尽时，把第二轮的失败原样交回上层（异常或错误 chunk 的既有语义不变）。
    """
    for attempt in range(2):
        gen = None
        committed = False
        buf: list = []
        try:
            gen = make_gen()
            async for ck in gen:
                if _is_error_chunk(ck):
                    if committed or attempt >= 1:
                        # 不能重试 / 已重试过：原样透传该错误 chunk
                        if attempt >= 1:
                            _STATS["exhausted"] += 1
                        yield ck
                        return
                    raise UpstreamResultError(_error_text(ck))
                if committed:
                    yield ck
                    continue
                if _chunk_has_substance(ck):
                    committed = True
                    for b in buf:
                        yield b
                    buf.clear()
                    yield ck
                else:
                    buf.append(ck)
            # 正常结束：把仅元数据的缓冲原样吐出（保持与改动前一致的观感）
            if not committed:
                for b in buf:
                    yield b
            if attempt > 0:
                _STATS["recovered"] += 1
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if committed or attempt >= 1:
                if attempt >= 1:
                    _mark_exhausted(e)
                    _STATS["exhausted"] += 1
                raise
            if not is_retryable_error(e):
                _STATS["skipped"] += 1
                raise
            _STATS["retried"] += 1
            logger.warning("上游流式调用失败，自动重试一次（%s）：%s: %s",
                           label, type(e).__name__, str(e)[:300])
            await _sleep_before_retry(delay)
        finally:
            if gen is not None:
                try:
                    await gen.aclose()
                except Exception:
                    pass
    raise RuntimeError("retry loop exhausted")  # pragma: no cover


class RetryingAdapter:
    """BaseAdapter 代理：只给 chat_completion / stream_chat_completion 加一次重试。

    其余属性与方法（list_models / health_check / timeout / last_proxy_url /
    适配器私有辅助方法）全部透传给原始适配器。
    """

    def __init__(self, inner, *, delay: float = 1.0):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_delay", delay)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        if name in ("_inner", "_delay"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_inner"), name, value)

    def _label(self, request) -> str:
        try:
            return str(getattr(request, "model", "") or "?")
        except Exception:
            return "?"

    async def chat_completion(self, request, api_key, base_url, extra_headers=None):
        inner = object.__getattribute__(self, "_inner")
        delay = object.__getattribute__(self, "_delay")
        return await _call_with_retry(
            lambda: inner.chat_completion(request, api_key, base_url, extra_headers),
            delay=delay, label=self._label(request),
        )

    async def stream_chat_completion(self, request, api_key, base_url, extra_headers=None):
        inner = object.__getattribute__(self, "_inner")
        delay = object.__getattribute__(self, "_delay")
        async for ck in _stream_with_retry(
            lambda: inner.stream_chat_completion(request, api_key, base_url, extra_headers),
            delay=delay, label=self._label(request),
        ):
            yield ck

    async def list_models(self, api_key, base_url, extra_headers=None):
        return await object.__getattribute__(self, "_inner").list_models(
            api_key, base_url, extra_headers)

    async def health_check(self, model, api_key, base_url, extra_headers=None, timeout=10):
        return await object.__getattribute__(self, "_inner").health_check(
            model, api_key, base_url, extra_headers, timeout)


class RetryingFreeExecutor:
    """free_tier executor 代理（opencode / mimo-free 等）。"""

    def __init__(self, inner, *, delay: float = 1.0):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_delay", delay)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        if name in ("_inner", "_delay"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_inner"), name, value)

    def _label(self, request) -> str:
        try:
            return str(getattr(request, "model", "") or "?")
        except Exception:
            return "?"

    async def execute_non_stream(self, request):
        inner = object.__getattribute__(self, "_inner")
        delay = object.__getattribute__(self, "_delay")
        return await _call_with_retry(
            lambda: inner.execute_non_stream(request), delay=delay, label=self._label(request))

    async def execute_stream(self, request):
        inner = object.__getattribute__(self, "_inner")
        delay = object.__getattribute__(self, "_delay")
        async for ck in _stream_with_retry(
            lambda: inner.execute_stream(request), delay=delay, label=self._label(request)):
            yield ck


def wrap_adapter(adapter, *, timeout=None):
    """按开关包装适配器；关闭 / 已包装 / 后台刷新（timeout 非空）时原样返回。"""
    if adapter is None or timeout is not None:
        return adapter
    enabled, delay = retry_settings()
    if not enabled:
        return adapter
    if isinstance(adapter, RetryingAdapter):
        return adapter
    _STATS["wrapped"] += 1
    return RetryingAdapter(adapter, delay=delay)


def wrap_free_executor(executor):
    """按开关包装 free_tier executor；关闭 / 已包装时原样返回。"""
    if executor is None:
        return executor
    enabled, delay = retry_settings()
    if not enabled:
        return executor
    if isinstance(executor, RetryingFreeExecutor):
        return executor
    _STATS["wrapped"] += 1
    return RetryingFreeExecutor(executor, delay=delay)
