"""推理入口全局并发闸（过载保护，2026-10-15）。

背景：同机客户端（artex）以 81 条并发流式请求持续打 /v1/chat/completions，
单条上游耗时 ~58s；旧实现无任何并发上限——洪峰时 DB 连接池（20+10）被
长请求占满（QueuePool timeout 30s，94% 请求 500）、内存随并发与排队线性
上涨（RSS 2.4GB 触发内核 OOM）。本中间件给「会产生上游调用」的入口设一个
全局在途上限：超出即短暂等待，等不到就快速失败 503 + Retry-After——让压力
在入口处泄掉，而不是打穿连接池/内存。

设计要点：
- 纯 ASGI 实现（不用 BaseHTTPMiddleware，避免额外缓冲/任务；body 不读取，
  等待者由 TCP 背压自然限制内存）
- 只拦生成类入口：POST /v1/chat/completions、/v1/messages、/v1/responses、
  /v1beta/models/*:generateContent|:streamGenerateContent，以及 /combo:<ref>/ 前缀形态
- 配置 config.concurrency：enabled / max_inflight / acquire_timeout_seconds
  （设置页与 /admin/api/concurrency 可改，改完即时生效——每请求读一次配置）
"""
from __future__ import annotations

import asyncio
import json

# 精确匹配的生成类路径（组合前缀剥掉后比较）
_GATED_EXACT = {"/v1/chat/completions", "/v1/messages", "/v1/responses"}

_DEFAULT_LIMIT = 128
_DEFAULT_TIMEOUT = 15.0


def _gated(path: str) -> bool:
    """该路径是否属于需要限流的推理入口。"""
    p = path or ""
    if p.startswith("/combo:"):
        # /combo:<ref><rest> → 只看 rest（与 ComboPrefixMiddleware 同语义）
        i = p.find("/", 7)
        p = p[i:] if i >= 0 else ""
    if p in _GATED_EXACT:
        return True
    # Gemini 原生入口：/v1beta/models/<model>:generateContent / :streamGenerateContent
    if ":generateContent" in p or ":streamGenerateContent" in p:
        return True
    return False


class InflightGateMiddleware:
    def __init__(self, app):
        self.app = app
        self._sem: asyncio.Semaphore | None = None
        self._size: int | None = None
        self.stats = {
            "admitted": 0,
            "rejected": 0,
            "waited": 0,      # 到达时闸已满（进入等待）的次数
            "inflight": 0,    # 当前在途
            "max_seen": 0,    # 峰值在途
        }
        global _instance
        _instance = self

    def _config(self):
        """每请求读一次配置（热改即时生效）；任何异常回退内置默认。"""
        try:
            from server.config import get_config
            c = getattr(get_config(), "concurrency", None)
            if c is not None:
                return (
                    bool(getattr(c, "enabled", True)),
                    int(getattr(c, "max_inflight", _DEFAULT_LIMIT) or 0),
                    float(getattr(c, "acquire_timeout_seconds", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT),
                )
        except Exception:
            pass
        return True, _DEFAULT_LIMIT, _DEFAULT_TIMEOUT

    def _semaphore(self, size: int) -> asyncio.Semaphore:
        """按当前配置取信号量；配置改大/改小时重建（旧信号量自然排空）。"""
        if self._sem is None or self._size != size:
            self._sem = asyncio.Semaphore(size)
            self._size = size
        return self._sem

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http"
                or scope.get("method") != "POST"
                or not _gated(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return

        enabled, limit, timeout = self._config()
        if not enabled or limit <= 0:
            await self.app(scope, receive, send)
            return

        sem = self._semaphore(limit)
        acquired = False
        if sem.locked():
            self.stats["waited"] += 1
        try:
            try:
                await asyncio.wait_for(sem.acquire(), timeout=max(0.1, timeout))
                acquired = True
            except asyncio.TimeoutError:
                self.stats["rejected"] += 1
                await self._reject(send, limit)
                return
            self.stats["admitted"] += 1
            self.stats["inflight"] += 1
            if self.stats["inflight"] > self.stats["max_seen"]:
                self.stats["max_seen"] = self.stats["inflight"]
            try:
                await self.app(scope, receive, send)
            finally:
                self.stats["inflight"] -= 1
        finally:
            if acquired:
                sem.release()

    async def _reject(self, send, limit: int):
        body = json.dumps({
            "error": {
                "message": (f"gateway overloaded: too many in-flight inference requests "
                            f"(limit={limit}); retry shortly"),
                "type": "overloaded_error",
                "code": "gateway_overloaded",
            }
        }, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("latin-1")),
                (b"retry-after", b"2"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


_instance: InflightGateMiddleware | None = None


def get_gate_stats() -> dict:
    """给管理页/监控读取的实时统计（未初始化时返回空）。"""
    return dict(_instance.stats) if _instance is not None else {}
