"""异常文本可读化 —— 让「空消息异常」不再以 `ConnectError: ` 形态落库。

背景（2026-10-10 生产实证）：
    tokenharbor 经 mihomo 代理时，TLS 握手阶段被对端掐断。httpcore 的
    `map_exceptions` 用 `raise to_exc(exc) from exc` 包装底层异常，而
    anyio.BrokenResourceError 的 str 是空串 —— 于是请求日志里只剩
    `ConnectError: `，用户与排障者都看不出发生了什么（历史库同类形态还有
    `ReadError: ` / `WriteTimeout: ` 等，各 19 条）。

本模块把「异常类型 + 消息（为空时沿异常链找底层类型名兜底）」统一成一行
可读文本，供 v1 / playground / passthrough / 模型刷新等所有落库与日志点复用。
另：传输层异常发生时若本请求经代理，追加「（本次经代理 <url 脱敏>）」——
用户看到 ConnectError 时需要一眼分清「上游故障 vs 代理节点抖动」。
"""
from __future__ import annotations

# 底层异常类型 → 人话（消息为空时的兜底描述）。
# 键名是类名字符串（不 import anyio/httpcore，避免依赖耦合）。
_EMPTY_MSG_HINTS = {
    "BrokenResourceError": "连接被对端中断（TLS 握手/写入阶段被掐断）",
    "ClosedResourceError": "连接已被对端关闭",
    "EndOfStream": "对端提前关闭连接（未返回完整响应）",
    "ConnectError": "无法建立连接",
    "ConnectTimeout": "连接超时",
    "ProxyError": "代理隧道建立失败",
    "ReadError": "连接读取失败（对端中断）",
    "ReadTimeout": "读取超时",
    "WriteError": "连接写入失败",
    "WriteTimeout": "写入超时",
    "RemoteProtocolError": "对端违反协议断开连接",
    "ServerDisconnectedError": "对端断开连接（未响应）",
}

# 传输层异常家族：命中才追加代理归属（业务异常不加，避免误导）。
_TRANSPORT_LIKE = frozenset({
    "ConnectError", "ConnectTimeout", "ProxyError", "ReadError", "ReadTimeout",
    "WriteError", "WriteTimeout", "RemoteProtocolError", "ServerDisconnectedError",
    "BrokenResourceError", "ClosedResourceError", "EndOfStream",
    "NetworkError", "TransportError", "ProtocolError", "TimeoutException",
    "ConnectionError", "ConnectionResetError", "ConnectionRefusedError",
    "TimeoutError", "OSError", "SSL错误", "SSLError",
})


def _cause_chain(e: BaseException, max_depth: int = 5) -> list:
    """沿 __cause__/__context__ 收集异常链（由近及远），最多 max_depth 层。"""
    out = []
    c = e.__cause__ or e.__context__
    while c is not None and len(out) < max_depth:
        out.append(type(c).__name__)
        c = c.__cause__ or c.__context__
    return out


def _transport_like(e: BaseException) -> bool:
    names = [type(e).__name__]
    names.extend(_cause_chain(e))
    return any(n in _TRANSPORT_LIKE for n in names)


def _proxy_suffix() -> str:
    """本请求经代理时返回「（本次经代理 <脱敏 url>）」，否则空串。"""
    try:
        from server.core.proxy_pool import CURRENT_PROXY_URL, _mask_url
        u = CURRENT_PROXY_URL.get()
        if not u:
            return ""
        try:
            u = _mask_url(u)
        except Exception:
            pass
        return f"（本次经代理 {u}）"
    except Exception:
        return ""


def err_text(e: BaseException) -> str:
    """异常 → '类型: 消息'；消息为空时沿异常链兜底生成可读描述。

    示例：
        err_text(httpx.ConnectError(""))            # 无因由
        -> "ConnectError: 无法建立连接"
        # httpcore 包装链 ConnectError ← BrokenResourceError（str 均为空）
        -> "ConnectError: 连接被对端中断（TLS 握手/写入阶段被掐断）；底层 BrokenResourceError"
        # 传输层异常且本次经代理（CURRENT_PROXY_URL 有值）
        -> "...（本次经代理 http://127.0.0.1:7890）"
    """
    name = type(e).__name__
    msg = str(e).strip()
    if msg:
        out = f"{name}: {msg}"
    else:
        chain = _cause_chain(e)
        out = ""
        for cname in reversed(chain):  # 最深层（最接近根因）优先，描述最具体
            hint = _EMPTY_MSG_HINTS.get(cname)
            if hint:
                out = f"{name}: {hint}" + (f"；底层 {cname}" if cname != name else "")
                break
        if not out:
            hint = _EMPTY_MSG_HINTS.get(name)
            if hint:
                out = f"{name}: {hint}"
            elif chain:
                out = f"{name}: 无详情（底层异常 {chain[-1]}）"
            else:
                out = f"{name}: 无详情（对端未返回错误信息）"
    if _transport_like(e):
        out += _proxy_suffix()
    return out
