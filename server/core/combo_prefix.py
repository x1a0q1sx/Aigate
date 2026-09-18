"""
Combo 前缀路由（组合即端点）

把「组合选择」从请求体的 model 字段挪到 URL 路径上：

    http://host:8000/combo:918/v1/chat/completions   → 强制走 918 组合
    http://host:918/v1/models                        → 列该组合全部模型
    http://host:8000/combo:my-fast/v1/messages       → Anthropic 协议同理
    http://host:8000/combo:my-fast/models            → /v1/models 简写

实现为纯路径改写中间件：剥掉 "/combo:<ref>" 前缀转发到既有 /v1 端点，
并把 ref 记入 request.state.combo_scope；v1_router 在鉴权后、缓存指纹前
将 model 字段改写为 "combo:<名称>"，因此 chat/completions、messages、
responses 三个协议入口零改动复用同一套组合路由/回退逻辑。

ref 支持组合名称或数字 id（解析在 v1_router 侧完成，那里有 DB）。
"""
from __future__ import annotations
import re
from urllib.parse import unquote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# /combo:<ref>[/剩余路径]；ref 不含 "/"，支持 URL 编码的组合名
_COMBO_PATH_RE = re.compile(r"^/combo:([^/]+)(/.*)?$")

# 前缀裸访问与 models 简写 → 统一落到 /v1/models
_MODELS_ALIASES = {"", "/", "/models", "/model", "/v1", "/v1/"}


class ComboPrefixMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        m = _COMBO_PATH_RE.match(request.url.path)
        if not m:
            return await call_next(request)
        ref = unquote(m.group(1)).strip()
        rest = m.group(2) or ""
        if rest in _MODELS_ALIASES:
            rest = "/v1/models"
        # 改写路由用 path；state 在下游 Request 间共享（同一 scope dict）
        request.scope["path"] = rest
        request.scope.setdefault("state", {})["combo_scope"] = ref
        return await call_next(request)
