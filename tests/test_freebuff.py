"""Freebuff（Codebuff 免费层）协议与适配器测试。

协议来源（双源交叉核对）：
  1. pingmike2/freebuff2api-wokers 的 worker.js / server.js（社区逆向，含 2026-09 实测）
  2. CodebuffAI/freebuff 官方源码（free-agents.ts / freebuff-session.ts /
     freebuff-streak.ts / freebuff-countries.ts / use-freebuff-streak-query.ts）

每个用例锁死一条「反直觉、改错就静默出问题」的硬约束；这些约束全部有源码依据。

⚠️ 本文件**不需要真实凭据、不发真实网络请求**：所有 HTTP 均被假客户端拦截。
"""
import asyncio
import json

import pytest

import server.core.freebuff as fb
import server.adapters.freebuff_adapter as fa
from server.adapters.freebuff_adapter import FreebuffAdapter


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """每个用例前清空适配器模块级缓存（session / run / 目录 / 锁）。

    ⚠️ 这些缓存 key 是 token 指纹，**跨用例会串**：不清的话前一个用例缓存的
    instanceId 会被后一个用例复用（表现为「session 状态莫名不对」）。
    """
    fa._SESSIONS.clear()
    fa._RUNS.clear()
    fa._LOCKS.clear()
    fa._CATALOG.clear()
    yield
    fa._SESSIONS.clear()
    fa._RUNS.clear()
    fa._LOCKS.clear()
    fa._CATALOG.clear()


# ── 假 httpx ─────────────────────────────────────────

def make_httpx(calls, responses):
    """假 httpx.AsyncClient。

    `responses`: [(status, json_data)] 按调用顺序出（GET/POST 共用一条队列）。
    适配器用例里 session/run/chat 是三个请求，按顺序出即可。
    """
    responses = list(responses)

    class _Resp:
        def __init__(self, status, data):
            self.status_code = status
            self._data = data if data is not None else {}
            self.content = json.dumps(self._data).encode()
            self.text = json.dumps(self._data)

        @property
        def is_success(self):
            return 200 <= self.status_code < 400

        def json(self):
            return self._data

        async def aread(self):
            return self.content

        async def aclose(self):
            return None

        async def aiter_lines(self):
            for line in (self._data.get("_sse") or []):
                yield line

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _next(self):
            return _Resp(*responses.pop(0)) if responses else _Resp(200, {})

        async def get(self, url, headers=None, params=None):
            calls.append({"m": "GET", "url": url, "headers": headers or {}})
            return self._next()

        async def post(self, url, headers=None, json=None, content=None):
            calls.append({"m": "POST", "url": url, "headers": headers or {},
                          "json": json, "content": content})
            return self._next()

        async def request(self, method, url, headers=None, json=None):
            calls.append({"m": method, "url": url, "headers": headers or {}})
            return self._next()

        def build_request(self, method, url, headers=None, content=None):
            calls.append({"m": method, "url": url, "headers": headers or {},
                          "content": content})
            return {"m": method, "url": url, "headers": headers, "content": content}

        async def send(self, req, stream=False):
            calls.append({"m": "SEND", "url": req.get("url"),
                          "content": req.get("content")})
            return self._next()

        async def aclose(self):
            return None

    return _Client


def _patch(monkeypatch, calls, responses, module=None):
    target = module or fb
    monkeypatch.setattr(target.httpx, "AsyncClient", make_httpx(calls, responses))


# ── 纯函数：三条「不说就静默降级」的硬约束 ──────────────

def test_buffy_prefix_injected_byte_exact():
    """system 必须以 Buffy 开头（官方 hasFreebuffRootSystemPromptOpening 字节级校验）。

    缺了会 403 free_mode_cli_required；旧的 `[System Override...]` 绕过已被官方修补。
    """
    msgs = fb.normalize_messages([
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
    ])
    assert msgs[0]["content"].startswith(fb.BUFFY)
    assert "You are a helpful assistant." in msgs[0]["content"]


def test_buffy_injected_when_no_system_message():
    """没有 system 消息时要**补一条**（不能只在已有 system 上加工）。"""
    msgs = fb.normalize_messages([{"role": "user", "content": "hi"}])
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == fb.BUFFY


def test_buffy_not_duplicated_when_already_present():
    msgs = fb.normalize_messages([
        {"role": "system", "content": fb.BUFFY + " extra"},
    ])
    assert msgs[0]["content"].count(fb.BUFFY) == 1


def test_harness_markers_stripped():
    """外域 harness 身份短语命中即**静默降级模型**（官方 FOREIGN_HARNESS_SYSTEM_PROMPTS）。

    必须用等义中性文本替换（不是删除），否则会留下破碎语句。
    """
    out = fb.strip_harness_markers("You are Claude Code, Anthropic's official CLI")
    assert "You are Claude Code" not in out
    assert "Anthropic's official CLI" not in out
    assert "coding assistant" in out


def test_developer_role_normalized_to_system():
    msgs = fb.normalize_messages([{"role": "developer", "content": "x"}])
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith(fb.BUFFY)


def test_tool_names_prefixed_and_signature_tool_injected():
    """工具名必须加 mcp__ 前缀 + 注入官方 decide 签名工具。

    不加：外域工具名表命中 → 静默降级；不注入签名工具：foreign_toolset 判定
    「带 tools 但无 genuine 签名工具」→ 同样降级。
    """
    payload = fb.apply_tool_disguise({
        "tools": [{"type": "function", "function": {"name": "read_file"}}]})
    names = [t["function"]["name"] for t in payload["tools"]]
    assert "mcp__read_file" in names
    assert fb.SIGNATURE_TOOL_NAME in names


def test_tool_name_roundtrip_including_client_mcp_prefix():
    """客户端自带的 mcp__xxx 上行变 mcp__mcp__xxx，还原后仍是 mcp__xxx（一一映射）。"""
    wire = fb.to_wire_tool_name("mcp__server__tool")
    assert wire == "mcp__mcp__server__tool"
    assert fb.from_wire_tool_name(wire) == "mcp__server__tool"


def test_tool_choice_renamed_alongside_tools():
    """tool_choice 指名工具时必须同步改名，否则上游匹配不到工具定义。"""
    payload = fb.apply_tool_disguise({
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "tool_choice": {"type": "function", "function": {"name": "read_file"}},
    })
    assert payload["tool_choice"]["function"]["name"] == "mcp__read_file"


def test_no_tools_no_disguise():
    """没有 tools 时不动（官方只对带 tools 的请求做外域检测）。"""
    payload = fb.apply_tool_disguise({"messages": []})
    assert "tools" not in payload


def test_restore_tool_names_in_delta_and_message():
    obj = {"choices": [{"delta": {"tool_calls": [
        {"function": {"name": "mcp__read_file"}}]}}]}
    fb.restore_tool_names(obj)
    assert obj["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "read_file"
    obj2 = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "mcp__decide"}}]}}]}
    fb.restore_tool_names(obj2)
    assert obj2["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "decide"


def test_agent_mapping_and_fallback():
    """模型→agent 映射照官方 FREEBUFF_ROOT_AGENT_ID_BY_MODEL；未知模型走 base2-free。"""
    assert fb.agent_for_model("deepseek/deepseek-v4-flash") == "base2-free-deepseek-flash"
    assert fb.agent_for_model("mimo/mimo-v2.5") == "base2-free-mimo"
    assert fb.agent_for_model("brand-new/model") == "base2-free"


def test_trace_session_id_stable_within_window_and_uuid_shape():
    """trace id 在同一 30 分钟窗口内恒定、形状是 UUID v4（官方跨轮复用语义）。"""
    a = fb.trace_session_id("tok")
    b = fb.trace_session_id("tok")
    assert a == b
    assert len(a) == 36 and a[14] == "4" and a[8] == "-"
    assert fb.trace_session_id("other-token") != a


# ── build_chat_payload：上游门控的硬约束 ────────────────

def test_chat_payload_always_streams():
    """上游**强制流式**：无论客户端要什么，出站 stream 恒为 true。"""
    p = fb.build_chat_payload({"model": "m", "stream": False}, "m", "run-1", "inst-1", "tok")
    assert p["stream"] is True
    assert p["stream_options"] == {"include_usage": True}


def test_chat_payload_metadata_and_stop():
    p = fb.build_chat_payload({"model": "m"}, "m", "run-1", "inst-1", "tok")
    md = p["codebuff_metadata"]
    assert md["run_id"] == "run-1"
    assert md["cost_mode"] == "free"
    assert md["freebuff_instance_id"] == "inst-1"
    assert md["client_id"] and md["trace_session_id"]
    assert p["stop"] == [fb.CB_EASP_STOP]
    assert p["provider"] == {"data_collection": "deny"}


def test_chat_payload_respects_client_stop():
    p = fb.build_chat_payload({"model": "m", "stop": ["END"]}, "m", "r", "i", "t")
    assert p["stop"] == ["END"]


def test_chat_payload_drops_unknown_keys():
    """只透传上游认识的键（多余字段会被上游拒或干扰归因）。"""
    p = fb.build_chat_payload({"model": "m", "n": 3, "logit_bias": {"1": 0.5}},
                              "m", "r", "i", "t")
    assert "n" not in p
    assert p["logit_bias"] == {"1": 0.5}


# ── SSE 解析 ─────────────────────────────────────────

def test_parse_sse_unwraps_data_envelope():
    """上游可能把 chunk 包在 {"data": {...}} 信封里（worker.js 实测）。"""
    line = 'data: {"data": {"id": "x", "choices": [{"delta": {"content": "hi"}}]}}'
    obj = fb.parse_sse_data(line)
    assert obj["id"] == "x"
    assert obj["choices"][0]["delta"]["content"] == "hi"


def test_parse_sse_passthrough_plain_chunk():
    line = 'data: {"id": "y", "choices": []}'
    assert fb.parse_sse_data(line)["id"] == "y"


def test_parse_sse_ignores_done_and_non_data_lines():
    assert fb.parse_sse_data("data: [DONE]") is None
    assert fb.parse_sse_data("event: ping") is None
    assert fb.parse_sse_data("data: {bad json") is None


# ── session / run 生命周期 ─────────────────────────────

def test_create_session_returns_instance_and_sends_model_header(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {"status": "active", "instanceId": "inst-9",
                                       "model": "m", "expiresAt": "2026-09-26T20:00:00Z"})])
    out = asyncio.run(fb.create_session("tok", "m"))
    assert out["instance_id"] == "inst-9"
    assert calls[0]["headers"]["x-freebuff-model"] == "m"
    assert calls[0]["headers"]["x-freebuff-instance-id"]


def test_create_session_410_raises_model_unavailable(monkeypatch):
    """410 model_unavailable：官方已下线该模型 —— 全局失败，不换号不换模型。"""
    calls = []
    _patch(monkeypatch, calls, [(410, {"error": "model_unavailable", "message": "gone"})])
    with pytest.raises(fb.ModelUnavailable):
        asyncio.run(fb.create_session("tok", "m"))


def test_create_session_403_banned_is_terminal(monkeypatch):
    """403 + status=banned 是终态（不可恢复），必须与普通错误区分。"""
    calls = []
    _patch(monkeypatch, calls, [(403, {"status": "banned"})])
    with pytest.raises(fb.SessionError) as ei:
        asyncio.run(fb.create_session("tok", "m"))
    assert ei.value.terminal is True


def test_create_session_401_is_terminal(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(401, {"error": "Invalid API key"})])
    with pytest.raises(fb.SessionError) as ei:
        asyncio.run(fb.create_session("tok", "m"))
    assert ei.value.terminal is True


def test_create_session_429_reports_quota(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(429, {"message": "daily limit reached"})])
    with pytest.raises(fb.SessionError) as ei:
        asyncio.run(fb.create_session("tok", "m"))
    assert ei.value.status == 429
    assert ei.value.terminal is False


def test_query_snapshot_sends_include_unused_rate_limits(monkeypatch):
    """快照头是规范名（旧裸名上游不识别）—— 缺了拿不到 freebucks。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {"status": "none", "freebucks": {"balance": 5}})])
    snap, err = asyncio.run(fb.query_snapshot("tok"))
    assert err == ""
    assert calls[0]["headers"]["x-freebuff-include-unused-rate-limits"] == "1"
    assert snap["status_code"] == 200


def test_start_cli_login_returns_url_and_hash(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {"loginUrl": "https://x/login?auth_code=abc",
                                       "fingerprintHash": "h", "expiresAt": 1,
                                       "expiresInMs": 3600000})])
    info, err = asyncio.run(fb.start_cli_login())
    assert err == ""
    assert info["login_url"].startswith("https://")
    assert info["fingerprint_id"].startswith("codebuff-cli-")
    assert calls[0]["json"]["fingerprintId"] == info["fingerprint_id"]


def test_poll_cli_login_pending_on_401(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(401, {"error": "not logged in"})])
    user, err, pending = asyncio.run(fb.poll_cli_login({"fingerprint_id": "f",
                                                        "fingerprint_hash": "h",
                                                        "expires_at": 1}))
    assert user is None and pending is True


def test_poll_cli_login_aborts_on_400(monkeypatch):
    """400 = 授权请求已失效（终态），必须停止轮询（不能一直等）。"""
    calls = []
    _patch(monkeypatch, calls, [(400, {"error": "expired"})])
    user, err, pending = asyncio.run(fb.poll_cli_login({"fingerprint_id": "f",
                                                        "fingerprint_hash": "h",
                                                        "expires_at": 1}))
    assert user is None and pending is False


def test_poll_cli_login_success_returns_user(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, [(200, {"user": {"authToken": "at-1", "email": "a@b.c",
                                                "id": "u1"}})])
    user, err, pending = asyncio.run(fb.poll_cli_login({"fingerprint_id": "f",
                                                        "fingerprint_hash": "h",
                                                        "expires_at": 1}))
    assert user["authToken"] == "at-1" and pending is False


# ── 适配器：session 复用 / 串行 / 错误语义 ─────────────

def _patch_adapter(monkeypatch, calls, responses):
    _patch(monkeypatch, calls, responses, module=fa)


def test_adapter_reuses_cached_session(monkeypatch):
    """instanceId 复用：剩 >60s 时不重复 POST（每次 POST 都真扣 Freebucks）。"""
    calls = []
    _patch_adapter(monkeypatch, calls, [
        # 第 1 轮：POST session + 两次 start_run（主 + 子）
        (200, {"status": "active", "instanceId": "inst-1", "model": "m",
               "expiresAt": "2099-01-01T00:00:00Z"}),
        (200, {"runId": "run-1"}),
        (200, {"runId": "run-2"}),
        (200, {"_sse": ['data: {"choices":[{"delta":{"content":"hi"}}]}']}),
        # 第 2 轮：只应发 chat（session 与 run 都缓存）
        (200, {"_sse": ['data: {"choices":[{"delta":{"content":"yo"}}]}']}),
    ])
    a = FreebuffAdapter()

    async def run():
        out1 = [c async for c in a.stream_chat_completion(
            {"model": "m", "messages": [{"role": "user", "content": "x"}]}, "tok", "")]
        out2 = [c async for c in a.stream_chat_completion(
            {"model": "m", "messages": [{"role": "user", "content": "x"}]}, "tok", "")]
        return out1, out2

    out1, out2 = asyncio.run(run())
    assert out1 and out2
    session_posts = [c for c in calls if c["m"] == "POST"
                     and c["url"].endswith("/api/v1/freebuff/session")]
    assert len(session_posts) == 1, "session 应复用，不得重复创建（每次创建都扣额度）"
    run_posts = [c for c in calls if c["m"] == "POST" and c["url"].endswith("/api/v1/agent-runs")]
    assert len(run_posts) == 2, "run 链应缓存（主+子只建一次）"


def test_adapter_serializes_same_token(monkeypatch):
    """同 token 并发必须串行（上游对单账号并发极敏感：429/428/排队超时）。"""
    calls = []
    inflight = {"max": 0, "cur": 0}

    _patch_adapter(monkeypatch, calls, [
        (200, {"status": "active", "instanceId": "inst-1", "model": "m",
               "expiresAt": "2099-01-01T00:00:00Z"}),
        (200, {"runId": "run-1"}),
        (200, {"runId": "run-2"}),
        (200, {"_sse": ['data: {"choices":[{"delta":{"content":"a"}}]}']}),
        (200, {"_sse": ['data: {"choices":[{"delta":{"content":"b"}}]}']}),
    ])

    orig_open = FreebuffAdapter._open_stream

    async def slow_open(self, *a, **k):
        inflight["cur"] += 1
        inflight["max"] = max(inflight["max"], inflight["cur"])
        try:
            await asyncio.sleep(0.05)
            return await orig_open(self, *a, **k)
        finally:
            inflight["cur"] -= 1

    monkeypatch.setattr(FreebuffAdapter, "_open_stream", slow_open)
    a = FreebuffAdapter()

    async def run():
        async def one():
            return [c async for c in a.stream_chat_completion(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]}, "tok", "")]
        return await asyncio.gather(one(), one())

    asyncio.run(run())
    assert inflight["max"] == 1, "同 token 的会话建立必须串行（maxInFlight 应为 1）"


def test_adapter_retries_once_on_stale_session(monkeypatch):
    """428/409 = 缓存 session 已失效 → 清缓存重建一次（不是限流，不冷却）。"""
    calls = []
    _patch_adapter(monkeypatch, calls, [
        (200, {"status": "active", "instanceId": "inst-old", "model": "m",
               "expiresAt": "2099-01-01T00:00:00Z"}),
        (200, {"runId": "run-1"}),
        (200, {"runId": "run-2"}),
        # 第一次 chat 撞 428
        (428, {"error": "waiting_room_required"}),
        # 重建 session + 复用 run，再 chat 成功
        (200, {"status": "active", "instanceId": "inst-new", "model": "m",
               "expiresAt": "2099-01-01T00:00:00Z"}),
        (200, {"_sse": ['data: {"choices":[{"delta":{"content":"ok"}}]}']}),
    ])
    a = FreebuffAdapter()

    async def _run():
        return [c async for c in a.stream_chat_completion(
            {"model": "m", "messages": [{"role": "user", "content": "x"}]}, "tok", "")]

    out = asyncio.run(_run())
    assert out and out[0]["choices"][0]["delta"]["content"] == "ok"
    session_posts = [c for c in calls if c["m"] == "POST"
                     and c["url"].endswith("/api/v1/freebuff/session")]
    assert len(session_posts) == 2, "session 失效应重建一次"


def test_adapter_emits_error_chunk_on_failure(monkeypatch):
    calls = []
    _patch_adapter(monkeypatch, calls, [(401, {"error": "Invalid API key"})])
    a = FreebuffAdapter()

    async def _run():
        return [c async for c in a.stream_chat_completion(
            {"model": "m", "messages": []}, "tok", "")]

    out = asyncio.run(_run())
    assert out and out[0].get("error")
    assert "SessionError" in out[0]["error"]


def test_adapter_nonstream_aggregates_stream(monkeypatch):
    """非流式由适配器聚合上游 SSE（上游强制流式）。"""
    calls = []
    _patch_adapter(monkeypatch, calls, [
        (200, {"status": "active", "instanceId": "i", "model": "m",
               "expiresAt": "2099-01-01T00:00:00Z"}),
        (200, {"runId": "r1"}), (200, {"runId": "r2"}),
        (200, {"_sse": [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}',
        ]}),
    ])
    a = FreebuffAdapter()
    r = asyncio.run(a.chat_completion({"model": "m", "messages": []}, "tok", ""))
    assert r["choices"][0]["message"]["content"] == "Hello"
    assert r["usage"]["total_tokens"] == 7
    assert r["choices"][0]["finish_reason"] == "stop"


def test_adapter_health_check_never_posts(monkeypatch):
    """健康探测**只读**：POST /session 会真扣 Freebucks，绝不能用于探活。"""
    calls = []
    _patch_adapter(monkeypatch, calls, [(404, {"status": "none"})])
    a = FreebuffAdapter()
    res = asyncio.run(a.health_check("m", "tok", ""))
    assert res.status == "healthy"
    assert all(c["m"] != "POST" for c in calls), "健康检查不得发 POST"


def test_adapter_health_check_banned_is_unhealthy(monkeypatch):
    calls = []
    _patch_adapter(monkeypatch, calls, [(403, {"status": "banned"})])
    a = FreebuffAdapter()
    res = asyncio.run(a.health_check("m", "tok", ""))
    assert res.status == "unhealthy"
    assert "封禁" in res.error_message


def test_adapter_list_models_falls_back_to_builtin(monkeypatch):
    """在线目录失败 → 回退内置兜底表（绝不让刷新失败）。"""
    calls = []

    async def _no_net(proxy=None):
        return []

    monkeypatch.setattr(fb, "fetch_release_models", _no_net)
    a = FreebuffAdapter()
    models = asyncio.run(a.list_models("tok", ""))
    ids = [m.model_id for m in models]
    assert "mimo/mimo-v2.5" in ids
    assert "deepseek/deepseek-v4-flash" in ids
    assert all(m.is_free for m in models)


def test_adapter_list_models_uses_release_when_available(monkeypatch):
    calls = []

    async def _net(proxy=None):
        return ["vendor/new-model", "mimo/mimo-v2.5"]

    monkeypatch.setattr(fb, "fetch_release_models", _net)
    a = FreebuffAdapter()
    models = asyncio.run(a.list_models("tok", ""))
    ids = [m.model_id for m in models]
    assert ids == ["vendor/new-model", "mimo/mimo-v2.5"]
    # 兜底表里有展示名的用展示名，没有的用 id
    assert models[0].display_name == "vendor/new-model"
    assert models[1].display_name == "MiMo 2.5"


# ── 档位闸门（session_model_mismatch）────────────────────
# 事故（2026-09-28）：用户点了一个不在 limited 档的模型，报
#   HTTP 409 {"error":"session_model_mismatch","message":"Limited free access is
#   only available with GLM 5.3 Flash or DeepSeek V4.1 Flash or ..."}
# 旧代码把 409 一律当「session 脏」→ 清缓存重建重试一次。而 POST session 是
# **扣费**的，等于白扣一次额度再报同样的错，且失败的 session 继续占着单会话锁。
# 下面四条锁死：语义区分、不白重建、释放锁、报错可执行。

_NOT_ENTITLED_BODY = {
    "error": "session_model_mismatch",
    "message": ("Limited free access is only available with GLM 5.3 Flash or "
                "DeepSeek V4.1 Flash or MiMo 2.6 Flash or Solar Mini 4 or Solar Pro 4."),
}


def test_is_model_not_entitled_distinguishes_from_stale_session():
    """`session_model_mismatch` ≠ session 脏；后者仍走重建重试。"""
    assert fb.is_model_not_entitled(json.dumps(_NOT_ENTITLED_BODY)) is True
    # 真正的 session 失效（要重建重试）不得被误判成档位拒绝
    for stale in ({"error": "session_superseded"},
                  {"error": "waiting_room_required"},
                  {"error": "session_expired"}):
        assert fb.is_model_not_entitled(json.dumps(stale)) is False
    # 502 包装（旧上游）也识别
    assert fb.is_model_not_entitled("not valid for limited access") is True
    assert fb.is_model_not_entitled("") is False


def test_model_gate_message_is_actionable_and_keeps_upstream_text():
    msg = fb.model_gate_message("openai/gpt-6-luna", json.dumps(_NOT_ENTITLED_BODY))
    assert "openai/gpt-6-luna" in msg          # 指出是哪个模型
    assert "DeepSeek V4 Flash" in msg          # 给出可用模型（实测放行清单）
    assert "走代理" in msg                     # 给出换出口这条路
    assert "GLM 5.3 Flash" in msg              # 保留上游原文便于核对


def test_create_session_409_not_entitled_raises_actionable_error(monkeypatch):
    """session 阶段撞档位拒绝：报错要可执行，不能只说「模型锁冲突」。"""
    calls = []
    _patch(monkeypatch, calls, [(409, _NOT_ENTITLED_BODY)])
    with pytest.raises(fb.SessionError) as ei:
        asyncio.run(fb.create_session("tok", "openai/gpt-6-luna"))
    msg = str(ei.value)
    assert "openai/gpt-6-luna" in msg and "走代理" in msg
    assert "session_model_mismatch:" not in msg, "不应再吐裸上游错误码给用户"
    # 仍带 409 语义（上层据此判断不是 401/429）
    assert ei.value.status == 409


def test_create_session_409_stale_reports_real_code(monkeypatch):
    """非档位的 409 必须**如实回显上游错误码**，不得一律盖成 session_model_mismatch。

    上游对多种情况都回 409（实测 session_superseded / model_locked 等）。盖错码会把
    「另一个实例占了 session」误导成「模型不在此档」，用户就会去改模型而不是重试。
    """
    calls = []
    _patch(monkeypatch, calls, [
        (409, {"error": "session_superseded", "message": "taken over"}),
        # 释放占锁 session 后的重试（GET 拿持锁 id → DELETE）
        (200, {"status": "active", "instanceId": "inst-holder", "model": "m"}),
        (200, {}),
        # 重试仍冲突 → 抛错
        (409, {"error": "model_locked", "message": "still locked"}),
    ])
    with pytest.raises(fb.SessionError) as ei:
        asyncio.run(fb.create_session("tok", "m"))
    msg = str(ei.value)
    assert "session_model_mismatch" not in msg, "不得把其它 409 盖成档位拒绝"
    assert "model_locked" in msg, "必须回显真实错误码"
    assert ei.value.status == 409
    # 确实做了「GET 拿持锁 id → DELETE 释放」这一次重试
    deletes = [c for c in calls if c["m"] == "DELETE"]
    assert len(deletes) == 1


def test_create_session_releases_lock_and_retries_once(monkeypatch):
    """409 冲突 → 释放占锁 session 后重试一次即成功（照 worker.js 单会话锁恢复）。"""
    calls = []
    _patch(monkeypatch, calls, [
        (409, {"error": "session_superseded", "message": "Your session ended"}),
        (200, {"status": "active", "instanceId": "inst-holder", "model": "m"}),
        (200, {}),   # DELETE 响应
        (200, {"status": "active", "instanceId": "inst-new", "model": "m",
               "expiresAt": "2099-01-01T00:00:00Z"}),
    ])
    s = asyncio.run(fb.create_session("tok", "m"))
    assert s["instance_id"] == "inst-new"
    deletes = [c for c in calls if c["m"] == "DELETE"]
    assert len(deletes) == 1, "必须释放占锁 session"
    assert deletes[0]["headers"]["x-freebuff-instance-id"] == "inst-holder"


def test_create_session_gate_does_not_probe_lock(monkeypatch):
    """档位拒绝：不得走「释放锁」分支（那是给真冲突用的），直接给可执行提示。"""
    calls = []
    _patch(monkeypatch, calls, [(409, _NOT_ENTITLED_BODY)])
    with pytest.raises(fb.SessionError) as ei:
        asyncio.run(fb.create_session("tok", "openai/gpt-6-luna"))
    assert "走代理" in str(ei.value)
    assert not [c for c in calls if c["m"] == "DELETE"], "档位拒绝不该去删 session"


def test_adapter_does_not_rebuild_session_on_model_gate(monkeypatch):
    """chat 撞档位拒绝：**不得**清缓存重建重试（重建要扣费且必然再失败），
    并且必须 DELETE 掉刚建的 session 释放单会话锁。"""
    calls = []
    _patch_adapter(monkeypatch, calls, [
        (200, {"status": "active", "instanceId": "inst-1", "model": "openai/gpt-6-luna",
               "expiresAt": "2099-01-01T00:00:00Z"}),
        (200, {"runId": "run-1"}),
        (200, {"runId": "run-2"}),
        (409, _NOT_ENTITLED_BODY),
    ])
    a = FreebuffAdapter()

    async def _run():
        return [c async for c in a.stream_chat_completion(
            {"model": "openai/gpt-6-luna", "messages": [{"role": "user", "content": "x"}]},
            "tok", "")]

    out = asyncio.run(_run())
    assert out and out[0].get("error"), "应产出错误块而不是静默"
    assert "走代理" in out[0]["error"], "报错要可执行"
    session_posts = [c for c in calls if c["m"] == "POST"
                     and c["url"].endswith("/api/v1/freebuff/session")]
    assert len(session_posts) == 1, "档位拒绝不得重建 session（重建会再扣一次费）"
    deletes = [c for c in calls if c["m"] == "DELETE"
               and c["url"].endswith("/api/v1/freebuff/session")]
    assert len(deletes) == 1, "必须 DELETE 释放单会话锁，否则下次换模型也被顶掉"


def test_adapter_still_retries_on_genuine_stale_session(monkeypatch):
    """反向对照：真正的 session 失效仍要重建重试（别把两个分支搞反）。"""
    calls = []
    _patch_adapter(monkeypatch, calls, [
        (200, {"status": "active", "instanceId": "inst-old", "model": "m",
               "expiresAt": "2099-01-01T00:00:00Z"}),
        (200, {"runId": "run-1"}),
        (200, {"runId": "run-2"}),
        (409, {"error": "session_superseded", "message": "taken over"}),
        (200, {"status": "active", "instanceId": "inst-new", "model": "m",
               "expiresAt": "2099-01-01T00:00:00Z"}),
        (200, {"_sse": ['data: {"choices":[{"delta":{"content":"ok"}}]}']}),
    ])
    a = FreebuffAdapter()

    async def _run():
        return [c async for c in a.stream_chat_completion(
            {"model": "m", "messages": [{"role": "user", "content": "x"}]}, "tok", "")]

    out = asyncio.run(_run())
    assert out and out[0]["choices"][0]["delta"]["content"] == "ok"
    session_posts = [c for c in calls if c["m"] == "POST"
                     and c["url"].endswith("/api/v1/freebuff/session")]
    assert len(session_posts) == 2, "真 stale 仍应重建一次"


# ── 档位放行清单（只标注、不过滤）──────────────────────────

def test_entitled_model_ids_reads_snapshot_and_ignores_garbage():
    assert fb.entitled_model_ids({"rateLimitsByModel": {"a/b": {}, "c/d": {}}}) == ["a/b", "c/d"]
    # 无信息一律返回空表（调用方按「未知」处理，绝不据此删模型）
    assert fb.entitled_model_ids({}) == []
    assert fb.entitled_model_ids(None) == []
    assert fb.entitled_model_ids({"rateLimitsByModel": "nope"}) == []


def test_entitlement_cache_none_means_unknown(monkeypatch):
    """None（未知）与空集（明确一个都不放行）语义必须分开。"""
    fb._ENTITLED_CACHE.clear()
    assert fb.known_entitlement("tok-x") is None, "从未探测 = 未知"
    fb.note_entitlement("tok-x", {"rateLimitsByModel": {"m/1": {}}})
    assert fb.known_entitlement("tok-x") == frozenset({"m/1"})
    # 空快照不得覆盖已知值（否则一次异常响应会把整列模型误标成不可用）
    fb.note_entitlement("tok-x", {})
    assert fb.known_entitlement("tok-x") == frozenset({"m/1"})
    fb._ENTITLED_CACHE.clear()


def test_adapter_annotates_but_does_not_filter_by_entitlement(monkeypatch):
    """已知放行清单时：清单外模型加「当前档位不可用」标注，但**仍列在目录里**
    （出口换区/升档后即可用；且失败时由上游如实报错）。"""
    async def _net(proxy=None):
        return ["mimo/mimo-v2.5", "openai/gpt-6-luna"]

    monkeypatch.setattr(fb, "fetch_release_models", _net)
    fb._ENTITLED_CACHE.clear()
    fb.note_entitlement("tok", {"rateLimitsByModel": {"mimo/mimo-v2.5": {}}})
    a = FreebuffAdapter()
    models = asyncio.run(a.list_models("tok", ""))
    ids = [m.model_id for m in models]
    assert ids == ["mimo/mimo-v2.5", "openai/gpt-6-luna"], "不得过滤掉模型"
    by_id = {m.model_id: m.display_name for m in models}
    assert "不可用" not in by_id["mimo/mimo-v2.5"]
    assert "不可用" in by_id["openai/gpt-6-luna"]
    fb._ENTITLED_CACHE.clear()


def test_adapter_annotates_nothing_when_entitlement_unknown(monkeypatch):
    """从未探测过档位 → 一律不加标注（不能把「不知道」显示成「不可用」）。"""
    async def _net(proxy=None):
        return ["mimo/mimo-v2.5", "openai/gpt-6-luna"]

    monkeypatch.setattr(fb, "fetch_release_models", _net)
    fb._ENTITLED_CACHE.clear()
    a = FreebuffAdapter()
    models = asyncio.run(a.list_models("tok", ""))
    assert all("不可用" not in m.display_name for m in models)


def test_health_check_records_entitlement_for_later_listing(monkeypatch):
    """健康探测的快照顺带记下放行清单（零额外请求）→ 模型页随即能标注。"""
    calls = []
    _patch_adapter(monkeypatch, calls, [
        (200, {"status": "active", "accessTier": "limited",
               "rateLimitsByModel": {"mimo/mimo-v2.5": {}, "upstage/solar-pro4": {}}}),
    ])
    fb._ENTITLED_CACHE.clear()
    a = FreebuffAdapter()
    res = asyncio.run(a.health_check("m", "tok-hc", ""))
    assert res.status == "healthy"
    assert fb.known_entitlement("tok-hc") == frozenset({"mimo/mimo-v2.5", "upstage/solar-pro4"})
    # 只读：健康探测绝不 POST（POST 会扣 Freebucks）
    assert not [c for c in calls if c["m"] == "POST"]
    fb._ENTITLED_CACHE.clear()
