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


def is_risk_block_text(error) -> bool:
    """错误文本是否为上游「账号级风控拦截」。

    CodeBuddy 国际版 11128 是同一风控的多个文案变体（request illegal / 请求被拦截 /
    没通过渠道校验）；2026-10-16 实测：同 payload 换账号 12/12 通过、被拦账号 9/9 全败
    ——与请求内容无关，语义=「这个账号被上游拉黑」，冷却换号才有用。
    供 is_account_worthy_error 资格闸与账号冷却时长分档共用。"""
    text = str(error or "").lower()
    return any(m in text for m in (
        '"code":11128', "request illegal", "没通过渠道校验", "渠道校验"))


def credit_exhausted_hint(error) -> str:
    """余额/积分耗尽错误 → 给用户可操作的中文提示（空串=不是这类错误）。

    Cline 按量计费模型余额不足（402 insufficient_credits）时上游原文只说
    "Insufficient balance. Your Cline Credits balance is $0.00" —— 用户看不出
    「为什么订阅了 Cline Pass 还报余额不足」。实际上 Cline Pass 只覆盖
    `cline-pass/*` 订阅模型；公共目录模型（`deepseek/...`、`anthropic/...`）走
    Cline Credits 按量计费，余额为 0 就会 402。此提示用于错误文案末尾。"""
    text = str(error or "").lower()
    if "insufficient_credits" not in text and "insufficient balance" not in text:
        return ""
    if "cline" in text or "cline.bot" in text:
        return ("提示：Cline Pass 订阅只覆盖 `cline-pass/*` 订阅模型（不计 credits）；"
                "`deepseek/*`、`anthropic/*` 等公共目录模型走 Cline Credits 按量计费，"
                "余额为 0 即 402。请改用 cline-pass/* 模型，或到 cline.bot 充值。")
    return "提示：上游账户余额/积分不足，请充值或改用其他模型/账号。"


def combine_err_text(short, full) -> str:
    """合并「短形态 + 全量文本」：短形态保证 HTTP 状态码可提取（如 '429'），
    全量保证上游原文特征不被截断吃掉（如每日限额文案里的重置时间）。

    两者互相包含时返回其一；都有时用 ' || ' 连接（受 4000 字符防御上限）。
    用于账号记账：网关内错误以字符串流转，而各路径的短形态截断点（120/200/300）
    不一致——限额识别必须看到完整原文才不会退化成短冷却。"""
    s = str(short or "").strip()
    f = str(full or "").strip()
    if not f or f in s:
        return s
    if not s or s in f:
        return f[:4000]
    return (s + " || " + f)[:4000]


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
