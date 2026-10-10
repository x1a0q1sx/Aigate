# -*- coding: utf-8 -*-
"""空消息异常可读化 + 连接阶段重试预算（2026-10-10 tokenharbor 日志实证）。

**实证**：`tokenharbor/claude-haiku-5.5:free` 经 mihomo 代理时 TLS 握手被掐，
httpx 抛出的 ConnectError 消息为空（httpcore 把 str 为空的 BrokenResourceError
直接包进去），请求日志里只剩 `ConnectError: `；且"首字前重试一次"重试撞回
同一坏节点再次失败。本文件锁死两件事：

1. `server.core.error_text.err_text`：空消息沿异常链兜底出可读描述；
2. `v1_router._retry_budget_for`：连接阶段故障（连接未建立）允许 2 次重试，
   其余瞬态故障仍是 1 次。
"""
import httpx

import server.api.v1_router as vr
from server.core.error_text import err_text


class BrokenResourceError(Exception):
    """模拟 anyio.BrokenResourceError（str 为空串）。"""


def _empty_connect_error():
    """复刻 httpcore 包装链：httpx.ConnectError('') ← BrokenResourceError。"""
    try:
        try:
            raise BrokenResourceError()
        except BrokenResourceError as inner:
            raise httpx.ConnectError("") from inner
    except httpx.ConnectError as e:
        return e


# ─────────────── err_text：空消息兜底 ───────────────

def test_err_text_empty_message_falls_back_to_cause_chain():
    t = err_text(_empty_connect_error())
    assert t.startswith("ConnectError:")
    assert not t.endswith(": "), "空消息形态必须被兜底（旧形态 'ConnectError: '）"
    assert "中断" in t                      # 底层 BrokenResourceError → 人话
    assert "BrokenResourceError" in t      # 底层类型名保留，供排障


def test_err_text_keeps_nonempty_message():
    assert err_text(ValueError("boom")) == "ValueError: boom"


def test_err_text_empty_without_cause_uses_own_type_hint():
    assert err_text(httpx.ConnectError("")) == "ConnectError: 无法建立连接"


def test_err_text_known_empty_types():
    assert "读取失败" in err_text(httpx.ReadError(""))
    assert "写入超时" in err_text(httpx.WriteTimeout(""))


def test_err_text_generic_fallback_for_unknown_type():
    class Weird(Exception):
        pass
    t = err_text(Weird())
    assert t == "Weird: 无详情（对端未返回错误信息）"


# ─────────────── 重试预算：连接阶段 2 次 / 其余 1 次 ───────────────

def test_retry_budget_fast_conn_two_others_one():
    assert vr._retry_budget_for(httpx.ConnectError("x")) == 2
    assert vr._retry_budget_for(httpx.ProxyError("502 Bad Gateway")) == 2
    # ConnectTimeout 是黑洞型故障（每次等满超时），不给额外预算
    assert vr._retry_budget_for(httpx.ConnectTimeout("x")) == 1
    # RemoteProtocolError 可能是流中段断连，保守起见仍只重试一次
    assert vr._retry_budget_for(httpx.RemoteProtocolError("peer closed")) == 1
    assert vr._retry_budget_for(httpx.ReadTimeout("t")) == 1


# ─────────────── 与瞬态分类 / 落库文本的联动 ───────────────

def test_enriched_text_still_classified_transient():
    """兜底后的文本必须仍被瞬态正则识别（否则重试保护失效）。"""
    assert vr._is_transient_upstream_error(err_text(_empty_connect_error()))
    assert vr._is_transient_upstream_error(err_text(httpx.ReadError("")))
    assert vr._is_transient_upstream_error("RemoteProtocolError: 对端违反协议断开连接")


def test_full_err_text_no_empty_tail():
    """落库文本（_full_err_text）不得再出现空消息形态。"""
    out = vr._full_err_text(_empty_connect_error())
    assert "ConnectError" in out
    assert not out.rstrip().endswith(":")


def test_source_guard_stream_and_nonstream_retry_budget():
    """源码守卫：流式循环需容纳第 3 次尝试；两条路径都要用 _retry_budget_for。"""
    import inspect
    src = inspect.getsource(vr)
    assert "for _attempt_no in range(3)" in src, \
        "直连流式重试循环需容纳连接阶段第 3 次尝试（range(2)→range(3)）"
    assert src.count("_retry_budget_for(") >= 2, \
        "流式与非流式两条重试路径都必须使用 _retry_budget_for"
