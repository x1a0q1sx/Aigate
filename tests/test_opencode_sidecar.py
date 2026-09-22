# -*- coding: utf-8 -*-
"""OpenCode CLI sidecar 桥接：消息扁平化 / 响应转换 / 可用性探测

背景（2026-09-22 取证）：opencode.ai 免费层只认「官方 CLI 进程建立的会话」，
纯 HTTP 转发一律 403。方案 = 服务器常驻官方 CLI（`opencode serve`），AIGate 通过
其 HTTP API 转发（复刻 9router「让真 CLI 当上游客户端」的架构）。
"""
import pytest

from server.core import opencode_sidecar as sc


def test_flatten_messages_preserves_roles():
    out = sc.flatten_messages([
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在"},
        {"role": "user", "content": "继续"},
    ])
    assert "[system]\nSYS" in out
    assert "你好" in out and "在" in out and "继续" in out
    # 行为语义由 sidecar 的 aigate agent 提示词承担，这里不再叠加前置指令
    assert not out.startswith("You are the assistant")


def test_flatten_messages_typed_blocks():
    out = sc.flatten_messages([
        {"role": "user", "content": [
            {"type": "text", "text": "A"},
            {"type": "image_url", "image_url": {"url": "x"}},   # 非文本块忽略
            {"type": "text", "text": "B"},
        ]},
    ])
    assert "AB" in out


def test_flatten_messages_accepts_pydantic_chatmessage():
    """关键回归：executor 传的是 pydantic ChatMessage 对象，不是 dict。
    早先只判 isinstance(dict) 会静默跳过全部消息 → 空 prompt → CLI 回寒暄/无关内容。"""
    from server.schemas.chat import ChatMessage
    msgs = [
        ChatMessage(role="system", content="你是助手"),
        ChatMessage(role="user", content="HTTP 404 是什么"),
    ]
    out = sc.flatten_messages(msgs)
    assert "[system]\n你是助手" in out
    assert "HTTP 404 是什么" in out


def test_empty_prompt_is_rejected():
    """空 prompt 必须显式失败（否则 CLI 自由发挥，产生无关回复）"""
    import asyncio
    import pytest as _pytest
    with _pytest.raises(RuntimeError) as ei:
        asyncio.run(sc.chat_completion([], "mimo-v2.6-flash-free"))
    assert "无法从 messages 提取任何文本" in str(ei.value)


def test_flatten_messages_empty():
    assert sc.flatten_messages([]) == ""
    assert sc.flatten_messages([{"role": "user", "content": ""}]) == ""


def test_content_to_text_splits_reasoning_and_text():
    text, reasoning = sc._content_to_text([
        {"type": "reasoning", "text": "想一想"},
        {"type": "text", "text": "答案"},
        {"type": "text", "text": "！"},
    ])
    assert text == "答案！"
    assert reasoning == "想一想"


def test_to_openai_response_shape():
    msg = {
        "id": "msg_1", "type": "assistant",
        "time": {"created": 1790062450043, "completed": 1790062450359},
        "model": {"id": "mimo-v2.6-flash-free", "providerID": "opencode"},
        "content": [{"type": "reasoning", "text": "r"},
                    {"type": "text", "text": "ok"}],
        "finish": "stop",
        "tokens": {"input": 112, "output": 3, "reasoning": 15},
    }
    resp = sc._to_openai_response(msg, "mimo-v2.6-flash-free", "p")
    assert resp["object"] == "chat.completion"
    assert resp["model"] == "mimo-v2.6-flash-free"
    ch = resp["choices"][0]
    assert ch["message"]["content"] == "ok"
    assert ch["message"]["reasoning_content"] == "r"
    assert ch["finish_reason"] == "stop"
    assert resp["usage"] == {"prompt_tokens": 112, "completion_tokens": 3,
                             "total_tokens": 115}


@pytest.mark.asyncio
async def test_await_assistant_waits_for_completion(monkeypatch):
    """关键回归：assistant 消息是逐步填充的，必须等 time.completed 才返回，
    否则会拿到「只有空 reasoning 的中间态」→ 空回复。"""
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, items):
            self.status_code = 200
            self._items = items
        def json(self):
            return {"data": self._items}

    class FakeClient:
        async def get(self, url):
            calls["n"] += 1
            if calls["n"] < 3:
                # 中间态：有 assistant 但无 completed，content 里 text 为空
                return FakeResp([{
                    "type": "assistant", "id": "m1",
                    "time": {"created": 2000},
                    "content": [{"type": "reasoning", "text": ""}],
                }])
            return FakeResp([{
                "type": "assistant", "id": "m1",
                "time": {"created": 2000, "completed": 2100},
                "content": [{"type": "text", "text": "完成"}],
                "finish": "stop",
            }])

    msg, err = await sc._await_assistant(FakeClient(), "sid", since_ms=1000,
                                         poll_interval=0.01, deadline_s=2)
    assert err is None
    assert calls["n"] >= 3, "必须轮询到 completed 才返回"
    assert sc._content_to_text(msg["content"])[0] == "完成"


@pytest.mark.asyncio
async def test_await_assistant_surfaces_error():
    class FakeResp:
        status_code = 200
        def json(self):
            return {"data": [{
                "type": "assistant", "id": "m1",
                "time": {"created": 2000, "completed": 2001},
                "error": {"message": "upstream 403"},
            }]}

    class FakeClient:
        async def get(self, url):
            return FakeResp()

    msg, err = await sc._await_assistant(FakeClient(), "sid", since_ms=1000,
                                         poll_interval=0.01, deadline_s=2)
    assert err and "403" in str(err)


@pytest.mark.asyncio
async def test_await_assistant_ignores_stale_history():
    """回归：sidecar 偶发把历史会话内容带出（首轮见过返回无关的 "## Intuition ..."）。
    必须只认 created >= 本次提问时间 且已 completed 的 assistant。"""
    class FakeResp:
        def __init__(self, items):
            self.status_code = 200
            self._items = items
        def json(self):
            return {"data": self._items}

    class FakeClient:
        async def get(self, url):
            return FakeResp([
                # 历史残留（created 早于本次提问）——不得采纳
                {"type": "assistant", "id": "old",
                 "time": {"created": 500, "completed": 600},
                 "content": [{"type": "text", "text": "## Intuition ... linked list"}]},
                # 本轮，已完成
                {"type": "assistant", "id": "new",
                 "time": {"created": 2000, "completed": 2100},
                 "finish": "stop",
                 "content": [{"type": "text", "text": "正确答案"}]},
            ])

    msg, err = await sc._await_assistant(FakeClient(), "sid", since_ms=1000,
                                         poll_interval=0.01, deadline_s=2)
    assert err is None
    assert msg["id"] == "new"
    assert sc._content_to_text(msg["content"])[0] == "正确答案"


@pytest.mark.asyncio
async def test_user_msg_created_at_lookup():
    class FakeResp:
        status_code = 200
        def json(self):
            return {"data": [
                {"id": "msg_a", "type": "user", "time": {"created": 1234}},
                {"id": "msg_b", "type": "user", "time": {"created": 5678}},
            ]}

    class FakeClient:
        async def get(self, url):
            return FakeResp()

    c = FakeClient()
    assert await sc._user_msg_created_at(c, "sid", "msg_b") == 5678
    assert await sc._user_msg_created_at(c, "sid", "missing") is None
    assert await sc._user_msg_created_at(c, "sid", "") is None


@pytest.mark.asyncio
async def test_free_executor_opencode_uses_sidecar(monkeypatch):
    """opencode 免费执行器必须走 sidecar（纯 HTTP 已被上游封锁）"""
    from server.core import free_providers as fp
    called = {}

    async def fake_alive():
        return True

    async def fake_chat(messages, model_id, provider_id="opencode", **kw):
        called["model"] = model_id
        called["provider"] = provider_id
        return {"choices": [{"message": {"role": "assistant", "content": "x"}}]}

    monkeypatch.setattr(sc, "sidecar_alive", fake_alive)
    monkeypatch.setattr(sc, "chat_completion", fake_chat)

    ex = fp.get_free_executor("opencode")
    req = fp.ChatCompletionRequest(model="mimo-v2.6-flash-free",
                                   messages=[{"role": "user", "content": "hi"}],
                                   stream=False)
    out = await ex.execute_non_stream(req)
    assert called.get("model") == "mimo-v2.6-flash-free"
    assert out["choices"][0]["message"]["content"] == "x"


@pytest.mark.asyncio
async def test_free_executor_opencode_errors_when_sidecar_down(monkeypatch):
    from server.core import free_providers as fp

    async def dead():
        return False

    monkeypatch.setattr(sc, "sidecar_alive", dead)
    ex = fp.get_free_executor("opencode")
    req = fp.ChatCompletionRequest(model="m-x",
                                   messages=[{"role": "user", "content": "hi"}],
                                   stream=False)
    with pytest.raises(RuntimeError) as ei:
        await ex.execute_non_stream(req)
    assert "sidecar 未运行" in str(ei.value)


# ─────────────── 回归：工具权限挂起致 180s 超时（本次修复的真因） ───────────────
#
# 现场：客户端把编码 agent 的 system prompt 发过来 → 模型去调 bash → CLI 默认
# permission=ask → 无人批准 → assistant 消息永不 completed → 网关只能等到 deadline，
# 报 "TimeoutError: opencode sidecar 等待回复超时（180.0s）"。

def test_turn_state_groups_multi_leg_turn():
    """一轮可能有多条 assistant（先 tool-calls 后 stop），必须整轮归并。"""
    items = [
        {"type": "user", "id": "u1", "time": {"created": 2000}},
        {"type": "assistant", "id": "a1", "time": {"created": 2100, "completed": 2200},
         "finish": "tool-calls", "content": [{"type": "tool", "name": "bash"}]},
        {"type": "assistant", "id": "a2", "time": {"created": 2300, "completed": 2400},
         "finish": "stop", "content": [{"type": "text", "text": "答案"}]},
    ]
    st = sc._turn_state(items, since_ms=1000)
    assert len(st["turn"]) == 2
    assert st["pending"] == []
    assert st["finish"] == "stop"
    assert sc._pick_text(st)[0] == "答案"


def test_turn_state_excludes_stale_history():
    items = [
        {"type": "assistant", "id": "old", "time": {"created": 500, "completed": 600},
         "finish": "stop", "content": [{"type": "text", "text": "旧会话"}]},
        {"type": "assistant", "id": "new", "time": {"created": 2000, "completed": 2100},
         "finish": "stop", "content": [{"type": "text", "text": "本轮"}]},
    ]
    st = sc._turn_state(items, since_ms=1000)
    assert [m["id"] for m in st["turn"]] == ["new"]
    assert sc._pick_text(st)[0] == "本轮"


def test_pick_text_prefers_final_answer_over_intermediate_narration():
    """中间腿可能带「我换个方式试试」之类的过渡文本，最终腿才是答案。"""
    items = [
        {"type": "assistant", "id": "a1", "time": {"created": 2100, "completed": 2200},
         "finish": "tool-calls",
         "content": [{"type": "text", "text": "bash 失败了，我换 glob"}, {"type": "tool", "name": "glob"}]},
        {"type": "assistant", "id": "a2", "time": {"created": 2300, "completed": 2400},
         "finish": "stop", "content": [{"type": "reasoning", "text": "想"}, {"type": "text", "text": "最终答案"}]},
    ]
    text, reasoning = sc._pick_text(sc._turn_state(items, since_ms=1000))
    assert text == "最终答案"
    assert reasoning == "想"


def test_turn_state_marks_pending_when_incomplete():
    items = [{"type": "assistant", "id": "a1", "time": {"created": 2100},
              "content": [{"type": "reasoning", "text": ""}]}]
    st = sc._turn_state(items, since_ms=1000)
    assert len(st["pending"]) == 1


def test_merge_turn_outputs_single_message_with_summed_usage():
    items = [
        {"type": "assistant", "id": "a1", "sessionID": "s", "time": {"created": 2100, "completed": 2200},
         "finish": "tool-calls", "tokens": {"input": 100, "output": 10},
         "content": [{"type": "text", "text": "过渡"}]},
        {"type": "assistant", "id": "a2", "sessionID": "s", "time": {"created": 2300, "completed": 2400},
         "finish": "stop", "tokens": {"input": 120, "output": 7},
         "content": [{"type": "text", "text": "答案"}]},
    ]
    merged = sc._merge_turn(sc._turn_state(items, since_ms=1000))
    assert merged["finish"] == "stop"           # 必须报 stop，下游才不判「未完成」
    assert sc._content_to_text(merged["content"])[0] == "答案"
    assert merged["tokens"] == {"input": 120, "output": 17}
    assert merged["_turn_count"] == 2


@pytest.mark.asyncio
async def test_await_assistant_returns_last_text_when_stuck_on_tool_calls(monkeypatch):
    """卡在 tool-calls 且无进展时，不再空等到 deadline：返回已有内容而非超时。"""
    monkeypatch.setattr(sc, "auto_reject_tools", lambda: False)
    monkeypatch.setattr(sc, "sidecar_poll_interval", lambda: 0.01)

    class FakeResp:
        status_code = 200
        def json(self):
            return {"data": [
                {"type": "assistant", "id": "a1", "time": {"created": 2100, "completed": 2200},
                 "finish": "tool-calls",
                 "content": [{"type": "text", "text": "无法执行命令"}, {"type": "tool", "name": "bash"}]},
            ]}

    class FakeClient:
        async def get(self, url):
            return FakeResp()

    import time as _t
    t0 = _t.monotonic()
    st = await sc._await_assistant_state(FakeClient(), "sid", since_ms=1000,
                                         poll_interval=0.01, deadline_s=30)
    assert _t.monotonic() - t0 < 25, "应提前返回，而不是等到 deadline"
    assert st["finish"] == "tool-calls"
    assert sc._pick_text(st)[0] == "无法执行命令"


@pytest.mark.asyncio
async def test_reject_pending_permissions_posts_reject(monkeypatch):
    """无人值守：挂起的权限请求必须被主动拒绝（这是真因的根治点）。"""
    posted = []

    class FakeResp:
        def __init__(self, payload):
            self.status_code = 200
            self._p = payload
        def json(self):
            return self._p

    class FakeClient:
        async def get(self, url):
            return FakeResp({"data": [
                {"id": "per_1", "action": "bash", "resources": ["ls -la"]},
                {"id": "per_2", "action": "glob", "resources": ["*.py"]},
            ]})
        async def post(self, url, json=None):
            posted.append((url, json))
            return FakeResp({})

    n = await sc.reject_pending_permissions(FakeClient(), "ses_x")
    assert n == 2
    assert all(p[1] == {"reply": "reject"} for p in posted)
    assert all("per_" in p[0] for p in posted)


@pytest.mark.asyncio
async def test_await_assistant_rejects_pending_then_finishes(monkeypatch):
    """权限挂起 → 自动拒绝 → 后续腿产出 stop，整体按成功返回（不再 180s 超时）。"""
    monkeypatch.setattr(sc, "auto_reject_tools", lambda: True)
    monkeypatch.setattr(sc, "sidecar_poll_interval", lambda: 0.01)
    state = {"rejected": False, "n": 0}

    class FakeResp:
        def __init__(self, payload, code=200):
            self.status_code = code
            self._p = payload
        def json(self):
            return self._p
        text = ""

    class FakeClient:
        async def get(self, url):
            state["n"] += 1
            if "/permission" in url:
                if state["rejected"]:
                    return FakeResp({"data": []})
                return FakeResp({"data": [{"id": "per_1", "action": "bash", "resources": ["ls"]}]})
            if not state["rejected"]:
                # 权限未批 → 只有 tool-calls 腿，且缺 completed（挂起态）
                return FakeResp({"data": [
                    {"type": "assistant", "id": "a1", "time": {"created": 2100},
                     "content": [{"type": "reasoning", "text": "让我跑一下"}, {"type": "tool", "name": "bash"}]},
                ]})
            return FakeResp({"data": [
                {"type": "assistant", "id": "a1", "time": {"created": 2100, "completed": 2150},
                 "finish": "tool-calls", "content": [{"type": "tool", "name": "bash"}]},
                {"type": "assistant", "id": "a2", "time": {"created": 2200, "completed": 2300},
                 "finish": "stop", "content": [{"type": "text", "text": "工具不可用，答案是 404"}]},
            ]})
        async def post(self, url, json=None):
            state["rejected"] = True
            return FakeResp({})

    st = await sc._await_assistant_state(FakeClient(), "sid", since_ms=1000,
                                         poll_interval=0.01, deadline_s=10)
    assert st["finish"] == "stop"
    text, _ = sc._pick_text(st)
    assert text == "工具不可用，答案是 404"


@pytest.mark.asyncio
async def test_close_session_uses_unprefixed_path(monkeypatch):
    """回归：DELETE 只挂在无 /api 前缀的路径上。

    带 /api 的路径会被 Web UI 兜底路由吃掉并回 200 HTML（看起来成功、会话其实还在），
    早先 SIDECAR_BASE + "/api/session/{sid}" 因此静默失效 → 会话在 sidecar 里堆积。
    """
    monkeypatch.setattr(sc, "sidecar_base", lambda: "http://127.0.0.1:4096")
    calls = []

    class FakeResp:
        def __init__(self, code, ctype, text=""):
            self.status_code = code
            self.headers = {"content-type": ctype}
            self.text = text

    class FakeClient:
        async def delete(self, url):
            calls.append(url)
            if url.endswith("/api/session/ses_1"):
                # Web UI 兜底：200 但是 HTML，不是真删除
                return FakeResp(200, "text/html", "<!doctype html><html>...")
            return FakeResp(200, "application/json", "{}")

    await sc._close_session(FakeClient(), "ses_1")
    assert calls[0].endswith("/session/ses_1"), "必须先用无前缀路径"
    assert not calls[0].endswith("/api/session/ses_1")


def test_bridge_agent_config_denies_all_tools():
    """CLI agent 定义：工具全部 deny（不能靠 tools:false，那会返回空回复）。"""
    cfg = sc.bridge_agent_config("aigate")
    a = cfg["aigate"]
    assert a["mode"] == "primary"
    assert all(v == "deny" for v in a["permission"].values())
    assert {"bash", "read", "glob", "grep", "edit", "write"} <= set(a["permission"])
    assert "tools" not in a


def test_ensure_bridge_agent_config_writes_and_is_idempotent(tmp_path):
    import json as _json
    p = tmp_path / "opencode.json"
    # ① 文件不存在 → 创建
    ok, note = sc.ensure_bridge_agent_config("aigate", path=str(p))
    assert ok, note
    cfg = _json.loads(p.read_text(encoding="utf-8"))
    assert cfg["agent"]["aigate"]["mode"] == "primary"
    assert all(v == "deny" for v in cfg["agent"]["aigate"]["permission"].values())
    # ② 再跑一次 → 保持就绪且不重复写
    before = p.read_text(encoding="utf-8")
    ok2, note2 = sc.ensure_bridge_agent_config("aigate", path=str(p))
    assert ok2 and "已就绪" in note2
    assert p.read_text(encoding="utf-8") == before
    # ③ 工具权限被改坏 → 自动修回
    cfg["agent"]["aigate"]["permission"]["bash"] = "ask"
    p.write_text(_json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    ok3, _ = sc.ensure_bridge_agent_config("aigate", path=str(p))
    assert ok3
    assert _json.loads(p.read_text(encoding="utf-8"))["agent"]["aigate"]["permission"]["bash"] == "deny"


def test_ensure_bridge_agent_config_preserves_other_agents(tmp_path):
    """只增改网关 agent，别的 agent 与用户配置不能被抹掉。"""
    import json as _json
    p = tmp_path / "opencode.json"
    p.write_text(_json.dumps({"theme": "dark", "agent": {"mine": {"prompt": "hi", "mode": "primary"}}}),
                 encoding="utf-8")
    ok, note = sc.ensure_bridge_agent_config("aigate", path=str(p))
    assert ok, note
    cfg = _json.loads(p.read_text(encoding="utf-8"))
    assert cfg["theme"] == "dark"
    assert cfg["agent"]["mine"] == {"prompt": "hi", "mode": "primary"}
    assert cfg["agent"]["aigate"]["mode"] == "primary"


def test_ensure_bridge_agent_config_survives_broken_json(tmp_path):
    """CLI 配置被写坏时不得抛异常（启动流程不能因此失败），且不覆盖用户文件。"""
    p = tmp_path / "opencode.json"
    p.write_text("{ not json", encoding="utf-8")
    ok, note = sc.ensure_bridge_agent_config("aigate", path=str(p))
    assert not ok
    assert "不是合法 JSON" in note
    assert p.read_text(encoding="utf-8") == "{ not json"


def test_sidecar_settings_read_from_config(monkeypatch):
    """配置优先级：config.yaml > 环境变量 > 内置默认。"""
    class FakeBridge:
        timeout_seconds = 90
        poll_interval_ms = 1500
        agent = "custom-agent"
        base_url = "http://127.0.0.1:5000"
        auto_reject_tools = False
        enabled = False

    class FakeCfg:
        opencode_bridge = FakeBridge()

    monkeypatch.setattr(sc, "_cfg", lambda: FakeCfg.opencode_bridge)
    assert sc.sidecar_timeout() == 90
    assert sc.sidecar_poll_interval() == 1.5
    assert sc.sidecar_agent() == "custom-agent"
    assert sc.sidecar_base() == "http://127.0.0.1:5000"
    assert sc.auto_reject_tools() is False
    assert sc.bridge_enabled() is False


def test_sidecar_settings_fall_back_to_defaults(monkeypatch):
    """配置段缺失/损坏时必须回退默认，绝不抛异常。"""
    monkeypatch.setattr(sc, "_cfg", lambda: None)
    monkeypatch.delenv("AIGATE_OPENCODE_TIMEOUT", raising=False)
    monkeypatch.delenv("AIGATE_OPENCODE_AGENT", raising=False)
    assert sc.sidecar_timeout() == 180
    assert sc.sidecar_agent() == "aigate"
    assert sc.auto_reject_tools() is True
    assert sc.bridge_enabled() is True


@pytest.mark.asyncio
async def test_free_executor_opencode_respects_disabled_bridge(monkeypatch):
    from server.core import free_providers as fp
    monkeypatch.setattr(sc, "bridge_enabled", lambda: False)
    ex = fp.get_free_executor("opencode")
    req = fp.ChatCompletionRequest(model="m-x",
                                   messages=[{"role": "user", "content": "hi"}],
                                   stream=False)
    with pytest.raises(RuntimeError) as ei:
        await ex.execute_non_stream(req)
    assert "已停用" in str(ei.value)
