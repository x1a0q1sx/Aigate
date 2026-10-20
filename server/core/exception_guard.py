"""兜底异常护栏 + 错误日志防洪（过载保护第二道，2026-10-15）。

背景：连接池耗尽时 sqlalchemy.exc.TimeoutError 从路由层一路穿透到 uvicorn，
每个请求打印约 180 行 traceback——实测 20 秒涨 590KB（约 2.5GB/天），日志
洪泛本身拖慢事件循环并淹没真正有用的信息。本护栏在最外层（UserMiddleware
栈最外，仍在 Starlette ServerErrorMiddleware 之内）捕获未处理异常：

- 资源类异常（DB 池超时 / DB 不可用 / 事件循环超时）→ 503 + Retry-After
  短 JSON 响应，客户端可直接重试；
- 其余异常 → 500 短 JSON（响应体不含内部细节）；
- 日志一律按 (异常类型+路径) 的 10 秒窗口限流：窗口内首条记录 类型/摘要/前
  6 帧栈，后续只累计计数、到下一窗口补记「+N suppressed」，单条日志也做了
  截断——不再出现每请求 180 行的 traceback 洪泛；
- 响应已开始推送（流式中途失败）→ 无法改状态码，限流记日志后静默收尾。

注册位置见 server/main.py（最后 add_middleware = 最外层，先于
ServerErrorMiddleware 捕获，异常不再冒泡到 uvicorn 的 traceback 打印）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback

logger = logging.getLogger("aigate.exception_guard")

_FLOOD_WINDOW = 10.0          # 同 (类型, 路径) 的日志窗口（秒）
_TB_HEAD_LINES = 6            # 单条日志保留的栈帧行数上限
_MSG_MAX = 300                # 异常摘要截断长度

# key -> (window_start_monotonic, suppressed_count)
_flood: dict[str, tuple[float, int]] = {}


def _classify(exc: BaseException) -> tuple[int, str, str]:
    """异常 → (HTTP 状态, 错误码, 面向客户端的短消息)。"""
    try:
        import sqlalchemy.exc as _se
        if isinstance(exc, _se.TimeoutError):
            return 503, "database_pool_timeout", \
                "gateway busy: database connection pool exhausted, retry shortly"
        if isinstance(exc, _se.OperationalError):
            return 503, "database_operational_error", \
                "gateway busy: database temporarily unavailable, retry shortly"
    except Exception:
        pass
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return 503, "timeout", "gateway timeout, retry shortly"
    return 500, "internal_error", "internal server error"


def _log_limited(key: str, message: str) -> int:
    """窗口限流日志。返回被压制的条数（0 = 本条已记录）。"""
    now = time.monotonic()
    start, suppressed = _flood.get(key, (0.0, 0))
    if now - start >= _FLOOD_WINDOW:
        _flood[key] = (now, 0)
        if suppressed:
            logger.error("%s (+%d suppressed in last %.0fs)", message, suppressed, _FLOOD_WINDOW)
        else:
            logger.error("%s", message)
        return 0
    _flood[key] = (start, suppressed + 1)
    return 1


class ExceptionGuardMiddleware:
    def __init__(self, app):
        self.app = app
        self.stats = {"handled": 0, "by_type": {}, "suppressed_logs": 0}
        global _instance
        _instance = self

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def _send_wrapper(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, _send_wrapper)
        except Exception as exc:  # noqa: BLE001 —— 护栏职责就是兜住一切
            self._record(exc, scope)
            status, code, message = _classify(exc)
            if started:
                # 流式已开始：状态码无法再改，尽力正常结束响应
                try:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                except Exception:
                    pass
                return
            body = json.dumps({
                "error": {"message": message, "type": "gateway_error", "code": code}
            }, ensure_ascii=False).encode("utf-8")
            try:
                await send({
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("latin-1")),
                        (b"retry-after", b"2"),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
            except Exception:
                pass

    def _record(self, exc: BaseException, scope) -> None:
        self.stats["handled"] += 1
        name = type(exc).__name__
        self.stats["by_type"][name] = self.stats["by_type"].get(name, 0) + 1
        path = scope.get("path", "?")
        head = "\n".join(traceback.format_exc().splitlines()[:_TB_HEAD_LINES])
        # 连接池超时是「池被占满」的判定信号：附上池实时占用，便于定位持有者
        # （2026-10-19 事故：只看 503 文案无法判断是池泄漏还是真并发峰值）
        extra = ""
        try:
            import sqlalchemy.exc as _se
            if isinstance(exc, _se.TimeoutError):
                from server.core.pool_watchdog import pool_state
                from server.db import engine as _engine
                _co, _cap = pool_state(_engine)
                extra = f" [pool checkedout={_co}/{_cap}]"
        except Exception:
            pass
        suppressed = _log_limited(
            f"{name}:{path}",
            f"unhandled {name} on {path}{extra}: {str(exc)[:_MSG_MAX]}\n{head}",
        )
        if suppressed:
            self.stats["suppressed_logs"] += 1


_instance: ExceptionGuardMiddleware | None = None


def get_guard_stats() -> dict:
    """给管理页/监控读取的实时统计（未初始化时返回空）。"""
    return dict(_instance.stats) if _instance is not None else {}
