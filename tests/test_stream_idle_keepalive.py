"""SSE 空闲心跳 + Qoder 排队可见性（2026-10-29）。

背景：Qoder 等上游在首个 token 之前会静默数十秒（上游排队 10605 / 长上下文
推理）。这段时间网关对客户端零字节，客户端或中间代理的空闲超时会掐断连接，
表现为「卡死 / 反复重连」。更糟的是本地首块超时（20s）与竞速判定（15s）会把
静默期误判成失败 —— 触发回退或再发一条同上游请求，把队列压得更满。

这里锁死三件事：
1. 空闲期间真的会补 `: keepalive` 注释帧；
2. 心跳**不会吞掉**任何上游 chunk，顺序不变；
3. 心跳**不会杀死**上游（不能对 anext 用 wait_for —— cancel 等于关流）。
"""

import asyncio

import pytest

import server.api.v1_router as vr


# ── 测试替身 ──────────────────────────────────────────

async def _gen(items, delays):
    """按 delays 交错产出 items 的异步生成器。delays 长度 = len(items)。"""
    for item, delay in zip(items, delays):
        if delay:
            await asyncio.sleep(delay)
        yield item


@pytest.mark.asyncio
async def test_emits_keepalive_during_idle_gap():
    """上游静默超过 interval → 期间必须出现 keepalive 注释帧。"""
    src = _gen(["first", "second"], [0, 0.30])

    out = []
    async for item in vr._stream_with_idle_keepalive(src, interval=0.05):
        out.append(item)

    # 两个真实 chunk 一个都不能少，且顺序不变
    real = [x for x in out if x != b": keepalive\n\n"]
    assert real == ["first", "second"]
    # 静默 0.30s / 心跳 0.05s → 至少补了 3 次以上
    heartbeats = [x for x in out if x == b": keepalive\n\n"]
    assert len(heartbeats) >= 3


@pytest.mark.asyncio
async def test_no_keepalive_when_upstream_is_fast():
    """上游持续有数据时不应插入任何心跳。"""
    src = _gen([1, 2, 3], [0, 0, 0])

    out = []
    async for item in vr._stream_with_idle_keepalive(src, interval=5.0):
        out.append(item)

    assert out == [1, 2, 3]


@pytest.mark.asyncio
async def test_heartbeat_does_not_cancel_upstream():
    """核心回归：心跳超时**不能**取消底层 anext。

    历史陷阱：用 asyncio.wait_for(anext(...)) 时超时会 cancel 那个 task，
    对上 httpx 流等于直接关连接 —— 一次心跳就把整个请求杀死。
    这里让生成器在自身内部记录是否收到 GeneratorExit / CancelledError。
    """
    state = {"finished": False, "cancelled": False}

    async def slow_source():
        try:
            await asyncio.sleep(0.25)
            yield "late-chunk"
            state["finished"] = True
        except (asyncio.CancelledError, GeneratorExit):
            state["cancelled"] = True
            raise

    out = []
    async for item in vr._stream_with_idle_keepalive(slow_source(), interval=0.05):
        out.append(item)

    real = [x for x in out if x != b": keepalive\n\n"]
    assert real == ["late-chunk"], "上游 chunk 必须完整送达"
    assert state["finished"] is True
    assert state["cancelled"] is False, "心跳不得取消上游生成器"


@pytest.mark.asyncio
async def test_interval_zero_is_passthrough():
    """interval<=0 表示关闭心跳：原样透传，不插入任何帧。"""
    src = _gen(["a", "b"], [0.12, 0])

    out = []
    async for item in vr._stream_with_idle_keepalive(src, interval=0):
        out.append(item)

    assert out == ["a", "b"]


@pytest.mark.asyncio
async def test_empty_stream_terminates_cleanly():
    """空流（上游立刻结束）必须正常收尾，不挂住、不吐心跳。"""
    async def empty():
        if False:
            yield None

    out = [x async for x in vr._stream_with_idle_keepalive(empty(), interval=0.05)]
    assert out == []


# ── 注释帧：格式化与「不算实质内容」 ──────────────────

def test_sse_comment_is_rendered_as_comment_frame():
    """__sse_comment 必须渲染成 SSE 注释行，而不是 data: JSON。

    客户端按 SSE 规范忽略 `:` 开头的行 —— 这样排队状态对客户端无副作用，
    但连接保持活跃，抓原始流的人也能看到在等什么。
    """
    frame = vr._format_sse_chunk({"__sse_comment": "qoder upstream queued: wait 30s"}, "m")
    assert frame.startswith(b": ")
    assert b"data:" not in frame
    assert frame.endswith(b"\n\n")
    assert b"queued" in frame


def test_sse_comment_newlines_are_flattened():
    """注释内容里的换行必须压平：否则一行注释会切断成多帧，破坏 SSE 格式。"""
    frame = vr._format_sse_chunk({"__sse_comment": "line1\nline2"}, "m")
    body = frame.decode("utf-8")
    # 除去结尾的 \n\n，正文内不应再有换行
    assert "\n" not in body.rstrip("\n")
    assert "line1 line2" in body


def test_sse_comment_is_not_substantive_content():
    """注释帧不得被判定为实质内容 —— 否则会在候选尚未真正输出时误锁候选。"""
    assert vr._chunk_has_substance({"__sse_comment": "queued"}) is False


def test_sse_comment_does_not_pollute_usage():
    """注释帧不得产生 usage 统计。"""
    assert vr._stream_usage_dict({"__sse_comment": "queued"}) == {}


def test_sse_comment_does_not_pollute_output_text():
    """注释帧不得计入输出文本（否则 completion token 估算会被污染）。"""
    chunks = [
        {"__sse_comment": "queued 30s"},
        {"choices": [{"delta": {"content": "hello"}}]},
    ]
    text = vr._output_text_from_chunks(chunks)
    assert "queued" not in text
    assert "hello" in text


# ── 源码守卫：接线点不能丢 ────────────────────────────

def test_both_stream_paths_are_wrapped():
    """直连流与 auto 级联流都必须接上心跳（漏一处就会有一条路径继续卡死）。"""
    import inspect

    src = inspect.getsource(vr)
    # 至少两处 StreamingResponse 用到了包装器
    assert src.count("_stream_with_idle_keepalive(") >= 3, \
        "期望定义 1 处 + 直连/auto 两处调用"
    assert src.count("_stream_with_idle_keepalive(wrap_stream()") >= 1
    assert src.count("_stream_with_idle_keepalive(cascade_stream()") >= 1


def test_keepalive_config_exists_and_is_hot_reloadable():
    """配置项必须存在且可被热改（放 Config 下，随 reload 生效）。"""
    from server.config import Config, StreamConfig

    cfg = Config()
    assert isinstance(cfg.stream, StreamConfig)
    assert cfg.stream.idle_keepalive_seconds == 15
    assert StreamConfig(idle_keepalive_seconds=0).idle_keepalive_seconds == 0
