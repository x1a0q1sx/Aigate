"""Admin 面板 JSON 响应 gzip 压缩（仅 /admin 路径）。

背景：/admin/api/models 全量目录裸传 2.4MB，组合路由/模型页首次加载
在公网带宽下要数秒；JSON gzip 约 10:1。只压 /admin：推理路径（/v1/ 等）
存在流式 SSE，部分客户端不透明解压，收益为零风险不为零，明确不碰。

门控条件（全部满足才压）：路径以 /admin 开头、客户端 Accept-Encoding 含
gzip、响应为 JSON、无既有 content-encoding、声明 content-length 且达到
阈值。依赖 content-length 存在即天然排除流式响应（分块传输无此头），
不会改变任何流式行为。
"""
import gzip


class AdminGZipMiddleware:
    MIN_SIZE = 1024

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/admin"):
            await self.app(scope, receive, send)
            return

        req_headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
        if "gzip" not in req_headers.get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return

        compress = False

        async def send_wrapper(message):
            nonlocal compress
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers", []))
                lower = {k.decode("latin-1").lower(): v.decode("latin-1")
                         for k, v in hdrs}
                cl = lower.get("content-length")
                compress = (
                    "json" in lower.get("content-type", "")
                    and not lower.get("content-encoding")
                    and cl is not None and int(cl) >= self.MIN_SIZE
                )
                if compress:
                    hdrs = [(k, v) for k, v in hdrs
                            if k.decode("latin-1").lower() != "content-length"]
                    hdrs.append((b"content-encoding", b"gzip"))
                    vary = lower.get("vary", "")
                    merged = "Accept-Encoding" if not vary else f"{vary}, Accept-Encoding"
                    hdrs = [(k, v) for k, v in hdrs
                            if k.decode("latin-1").lower() != "vary"]
                    hdrs.append((b"vary", merged.encode("latin-1")))
                    message["headers"] = hdrs
                await send(message)
            elif message["type"] == "http.response.body" and compress:
                body = message.get("body", b"")
                if body:
                    message["body"] = gzip.compress(body)
                await send(message)
            else:
                await send(message)

        await self.app(scope, receive, send_wrapper)
