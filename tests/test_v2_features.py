"""v2 路线新特性纯逻辑测试（A4 缓存 / E2 权重 / A1 转换器 / D1 密钥工具）"""
import json
import time

import pytest


# ─────────────── A4: 响应缓存 ───────────────

class _FakeReq:
    def __init__(self, model="m1", stream=False, temperature=None):
        self.model = model
        self.stream = stream
        self.temperature = temperature
        self.messages = [{"role": "user", "content": "hi"}]

    def model_dump(self):
        return {"model": self.model, "stream": self.stream,
                "temperature": self.temperature, "messages": self.messages}


def _enable_cache(monkeypatch, enabled=True, ttl=300, max_items=2):
    class _Cfg:
        response_cache = type("C", (), {"enabled": enabled, "ttl_seconds": ttl,
                                        "max_items": max_items, "max_body_bytes": 262144})()
    monkeypatch.setattr("server.core.response_cache.get_config", lambda: _Cfg())


def test_response_cache_hit_and_miss(monkeypatch):
    from server.core.response_cache import ResponseCache
    c = ResponseCache()
    _enable_cache(monkeypatch)
    req = _FakeReq()
    assert c.get(req) is None            # miss
    c.put(req, {"ok": True})
    assert c.get(req) == {"ok": True}    # hit
    assert c.get(_FakeReq(stream=True)) is None   # 流式不缓存
    assert c.get(_FakeReq(model="other")) is None  # 不同指纹


def test_response_cache_ttl_and_lru(monkeypatch):
    from server.core.response_cache import ResponseCache
    c = ResponseCache()
    _enable_cache(monkeypatch, ttl=0.01, max_items=2)
    r1, r2, r3 = _FakeReq("a"), _FakeReq("b"), _FakeReq("c")
    c.put(r1, {"i": 1})
    time.sleep(0.02)                     # r1 过期
    c.put(r2, {"i": 2})
    c.put(r3, {"i": 3})                  # 超容量淘汰最旧（r1 已过期）
    assert c.get(r1) is None             # TTL 过期
    assert c.get(r2) == {"i": 2}
    assert c.get(r3) == {"i": 3}


def test_response_cache_disabled(monkeypatch):
    from server.core.response_cache import ResponseCache
    c = ResponseCache()
    _enable_cache(monkeypatch, enabled=False)
    req = _FakeReq()
    c.put(req, {"ok": True})
    assert c.get(req) is None            # 关闭时完全不工作


# ─────────────── E2: combo 权重抽签 ───────────────

def test_pick_start_index_weighted_respects_weight():
    from server.core.combo_router import pick_start_index
    targets = [{"full_id": "a", "weight": 100}, {"full_id": "b", "weight": 0.001}]
    picks = {pick_start_index(targets, 1, "weighted") for _ in range(200)}
    assert picks == {0, 1} or picks == {0}   # 高权重几乎必中；允许极小概率出现 1
    zero = [{"full_id": "a", "weight": 100}, {"full_id": "b", "weight": 0}]
    assert pick_start_index(zero, 1, "weighted") == 0  # weight<=0 视为 1，不崩
    assert pick_start_index(targets, 1, "fallback") == 0
    assert pick_start_index(targets, 1, "round_robin") in (0, 1)


# ─────────────── A1: Gemini 转换器 ───────────────

def test_gemini_to_chat_basic_and_tools():
    from server.core.gemini_converter import gemini_to_chat
    body = {
        "systemInstruction": {"parts": [{"text": "be nice"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "hello"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "f", "args": {"x": 1}}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "f", "response": {"r": 2}}}]},
        ],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 99, "thinkingConfig": {"thinkingBudget": 8192}},
        "tools": [{"functionDeclarations": [{"name": "f", "description": "d", "parameters": {}}]}],
    }
    kwargs = gemini_to_chat("gemini-x", body)
    assert kwargs["model"] == "gemini-x"
    roles = [m["role"] for m in kwargs["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert kwargs["messages"][2]["tool_calls"][0]["function"]["name"] == "f"
    assert kwargs["messages"][3]["tool_call_id"] == "call_f"
    assert kwargs["temperature"] == 0.5 and kwargs["max_tokens"] == 99
    assert kwargs["reasoning_effort"] == "medium"
    assert kwargs["tools"][0]["type"] == "function"


def test_gemini_to_chat_inline_image():
    from server.core.gemini_converter import gemini_to_chat
    body = {"contents": [{"role": "user", "parts": [
        {"text": "look"}, {"inlineData": {"mimeType": "image/png", "data": "QUJD"}}]}]}
    kwargs = gemini_to_chat("m", body)
    content = kwargs["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,QUJD")


def test_chat_json_to_gemini_and_chunks():
    from server.core.gemini_converter import chat_json_to_gemini, chunk_to_gemini
    payload = {"choices": [{"message": {"content": "hi", "reasoning_content": "think"},
                            "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    out = chat_json_to_gemini(payload, "m1")
    assert out["candidates"][0]["content"]["parts"][0] == {"text": "think", "thought": True}
    assert out["candidates"][0]["content"]["parts"][1] == {"text": "hi"}
    assert out["candidates"][0]["finishReason"] == "STOP"
    assert out["usageMetadata"]["totalTokenCount"] == 5
    g = chunk_to_gemini({"choices": [{"delta": {"content": "a"}}]})
    assert g["candidates"][0]["content"]["parts"] == [{"text": "a"}]
    assert chunk_to_gemini({"choices": [{"delta": {}}]}) is None


# ─────────────── D1: 网关密钥工具 ───────────────

def test_gateway_key_hash_and_generate():
    from server.core.gateway_keys import generate_key, hash_key, _check_rpm
    k = generate_key()
    assert k.startswith("gk-") and len(k) > 40
    assert hash_key(k) == hash_key(k) and hash_key(k) != hash_key(k + "x")
    row = type("R", (), {"rpm_limit": 2, "id": 999, "name": "t"})()
    _check_rpm(row)
    _check_rpm(row)
    with pytest.raises(Exception):
        _check_rpm(row)                  # 第 3 次超 2/min


# ─────────────── 瞬态上游故障重试 + 规范错误对象 ───────────────

def test_transient_upstream_error_predicate():
    from server.api.v1_router import _is_transient_upstream_error
    # 可重试：5xx / 429 / 网络超时断连 / 站点准入拒绝
    assert _is_transient_upstream_error("HTTPStatusError: Client error '503 Service Unavailable' for url ...")
    assert _is_transient_upstream_error("HTTPStatusError: Client error '429 Too Many Requests'")
    assert _is_transient_upstream_error("upstream_stream_failed: cache-only admission rejected a cold request")
    assert _is_transient_upstream_error("httpx.ConnectError: connection reset")
    assert _is_transient_upstream_error("Request timed out after 30s")
    # 不可重试：参数错误 / 鉴权失败
    assert not _is_transient_upstream_error("HTTPStatusError: Client error '400 Bad Request'")
    assert not _is_transient_upstream_error("Invalid API key")
    assert not _is_transient_upstream_error("Model not found")
    assert not _is_transient_upstream_error("")


def test_api_error_object_shape():
    from server.api.v1_router import _api_error
    out = _api_error("upstream_stream_failed: 503", status=503)
    assert isinstance(out["error"], dict)                 # error 必须是对象
    assert out["error"]["type"] == "server_error"
    assert "503" in out["error"]["message"]
    out2 = _api_error("too many requests", status=429)
    assert out2["error"]["type"] == "rate_limit_error"
    out3 = _api_error("Model m not found", status=404)
    assert out3["error"]["type"] == "invalid_request_error"
    out4 = _api_error("combo exhausted", status=503, attempts=[{"error": "x"}])
    assert out4["error"]["attempts"] == [{"error": "x"}]  # 附加字段在 error 对象内


@pytest.mark.asyncio
async def test_responses_stream_translates_object_error():
    """对象格式的终态 error chunk（新格式）同样翻译为 response.failed。"""
    import json as _json
    from server.api.responses_router import _chat_to_responses_stream
    from fastapi.responses import StreamingResponse

    payload = _json.dumps({"error": {"message": "upstream_stream_failed: 503 Service Unavailable",
                                     "type": "server_error", "code": None}})
    async def gen():
        yield f"data: {payload}\n\n".encode()
        yield b"data: [DONE]\n\n"

    sr = _chat_to_responses_stream(StreamingResponse(gen(), media_type="text/event-stream"), "m1")
    body = b""
    async for piece in sr.body_iterator:
        body += piece if isinstance(piece, bytes) else piece.encode()
    text = body.decode("utf-8", "replace")
    assert "response.failed" in text
    assert "503 Service Unavailable" in text
    assert "response.completed" not in text


@pytest.mark.asyncio
async def test_write_batch_updates_pending_row_inplace(monkeypatch, tmp_path):
    """待响应行：完成态日志按 conversation_id 原位更新（单行），而非补插第二行。"""
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from server.models.base import Base
    from server.models.request_log import RequestLog

    db_file = tmp_path / "t.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("server.db.AsyncSessionLocal", SessionMaker)

    cid = "conv-test-1"
    async with SessionMaker() as db:
        db.add(RequestLog(conversation_id=cid, requested_model="m1", status="pending"))
        await db.commit()

    from server.core.log_queue import _write_batch
    await _write_batch([{
        "conversation_id": cid,
        "requested_model": "m1",
        "routed_provider": "p1", "routed_model": "m1",
        "status": "success",
        "latency_ms": 1234,
        "prompt_tokens": 10, "completion_tokens": 5,
    }])
    await asyncio.sleep(0)

    async with SessionMaker() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(RequestLog).where(RequestLog.conversation_id == cid))).scalars().all()
    assert len(rows) == 1, "应原位更新为单行，而不是补插"
    r = rows[0]
    assert r.status == "success" and r.latency_ms == 1234 and r.prompt_tokens == 10

    # 无 pending 行时照常插入（管理页直发等场景）
    await _write_batch([{"conversation_id": "conv-test-2", "status": "success"}])
    async with SessionMaker() as db:
        from sqlalchemy import select, func
        n2 = (await db.execute(select(func.count(RequestLog.id)).where(RequestLog.conversation_id == "conv-test-2"))).scalar()
    assert n2 == 1

    # 陈旧清扫：pending 行强制收尾为 error
    from server.core.log_queue import _sweep_stale_pending
    async with SessionMaker() as db:
        db.add(RequestLog(conversation_id="conv-test-3", status="pending"))
        await db.commit()
    swept = await _sweep_stale_pending(force=True)
    assert swept >= 1
    async with SessionMaker() as db:
        from sqlalchemy import select
        r3 = (await db.execute(select(RequestLog).where(RequestLog.conversation_id == "conv-test-3"))).scalar_one()
    assert r3.status == "error" and r3.error_type == "interrupted"
    await engine.dispose()


@pytest.mark.asyncio
async def test_responses_translator_closes_source_on_terminal_error():
    """终态错误提前返回时必须关闭底层 v1 流——其 finally 负责把失败请求写入日志。"""
    import asyncio
    from fastapi.responses import StreamingResponse
    from server.api.responses_router import _chat_to_responses_stream

    closed = {"v": False}

    async def source():
        try:
            yield b'data: {"error": {"message": "upstream 503", "type": "server_error", "code": null}}\n\n'
            yield b"data: [DONE]\n\n"
        finally:
            closed["v"] = True

    sr = _chat_to_responses_stream(StreamingResponse(source(), media_type="text/event-stream"), "m1")
    async for piece in sr.body_iterator:
        blob = piece if isinstance(piece, bytes) else piece.encode()
        if b"response.failed" in blob:
            break  # 模拟客户端在失败事件后停止消费
    await sr.body_iterator.aclose()  # StreamingResponse 收尾时关闭翻译器
    assert closed["v"], "底层流应被关闭（其 finally 负责落日志）"
