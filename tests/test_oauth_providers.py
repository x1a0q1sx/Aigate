"""CodeBuddy（CN/国际服）与 Cline OAuth + 请求方言测试。

对齐 9router 实测协议：
  - device poll：POST state_url?platform= → {code:0,data:{state,authUrl}}；GET token_url?state=
  - codebuddy refresh：X-Refresh-Token 头 + 空 JSON body
  - cline：authorize 无 client_id；回调 code = base64(JSON token)；JSON 刷新；
    JWT 出站补 workos: 前缀；非流式响应 success/data 信封解包
  - codebuddy：流式专属上游 → 网关聚合；CN agent system prompt 中性化；
    intl typed-blocks 形态；reasoning_summary 镜像
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs

import pytest

import server.core.oauth_client as oc
from server.core.oauth_client import OAuthClient, decode_cline_code, _expires_in_from
from server.core.oauth_registry import get_oauth_provider
from server.core.provider_quirks import (
    quirks_for, auth_token_for, transform_payload, unwrap_response,
)


class FakeCrypto:
    def encrypt(self, s):
        return "enc:" + (s or "")

    def decrypt(self, s):
        return (s or "").removeprefix("enc:")


def make_fake_httpx(calls, results=None):
    """httpx.AsyncClient 替身：记录调用，按队列返回预置响应。

    results: [(status, json_data)]；_collect_stream 用 stream() 场景单独传
    FakeStreamClient。"""
    results = list(results or [])

    class _Resp:
        def __init__(self, status=200, json_data=None):
            self.status_code = status
            self._json = json_data if json_data is not None else {}
            self.text = json.dumps(self._json)
            self.request = None
            self.reason_phrase = "OK"

        def json(self):
            return self._json

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _next(self):
            if results:
                s, j = results.pop(0)
                return _Resp(s, j)
            return _Resp()

        async def post(self, url, headers=None, content=None, json=None, data=None, params=None):
            calls.append({"method": "post", "url": url, "headers": headers or {},
                          "content": content, "json": json, "data": data})
            return self._next()

        async def get(self, url, headers=None, params=None):
            calls.append({"method": "get", "url": url, "headers": headers or {}, "params": params})
            return self._next()

    return _Client


# ── registry 完整性 ─────────────────────────────────────────

def test_registry_codebuddy_cn_intl_and_cline():
    cn = get_oauth_provider("codebuddy_cn")
    intl = get_oauth_provider("codebuddy_intl")
    cl = get_oauth_provider("cline")
    assert cn and intl and cl
    assert (cn.extra_params or {}).get("auth_mode") == "device_poll"
    assert cn.extra_params["refresh_style"] == "codebuddy"
    assert cn.extra_params["platform"] == "CLI"
    assert intl.extra_params["platform"] == "ide"
    assert "codebuddy.ai" in intl.token_url and "codebuddy.ai" in intl.api_base_url
    assert intl.extra_params["x_domain"] == "www.codebuddy.ai"
    ep = cl.extra_params or {}
    assert ep.get("auth_mode") == "cline" and ep.get("token_in_code") is True
    assert ep.get("refresh_style") == "cline"
    assert cl.use_pkce is False
    assert cl.api_base_url == "https://api.cline.bot/api/v1/chat/completions"


# ── cline base64-code 解码 ─────────────────────────────────

def test_decode_cline_code_success():
    exp = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    payload = {"accessToken": "eyJJWT.TOKEN.sig", "refreshToken": "rt-1",
               "expiresAt": exp, "email": "u@example.com"}
    import base64
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    # 去掉尾部的 = 填充：Cline 回调正是这种形态
    tok = decode_cline_code(b64.rstrip("="))
    assert tok is not None
    assert tok["access_token"] == "eyJJWT.TOKEN.sig"
    assert tok["refresh_token"] == "rt-1"
    assert 3600 < tok["expires_in"] <= 7200 + 5
    assert tok["scope"] == "email:u@example.com"


def test_decode_cline_code_garbage():
    assert decode_cline_code("not-a-code") is None
    assert decode_cline_code("") is None
    import base64
    assert decode_cline_code(base64.b64encode(b'{"no":"token"}').decode()) is None


def test_expires_in_from():
    assert _expires_in_from(None, 7200) == 7200
    future = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    assert 59 < _expires_in_from(future, None) <= 301
    assert _expires_in_from("garbage", 120) == 120


# ── cline authorize URL ────────────────────────────────────

def test_build_authorize_url_cline():
    c = OAuthClient(crypto=FakeCrypto(), redirect_override="http://gw.example:8000/admin/oauth/callback")
    url, state, verifier = c.build_authorize_url(get_oauth_provider("cline"))
    qs = parse_qs(urlparse(url).query)
    assert url.startswith("https://api.cline.bot/api/v1/auth/authorize?")
    assert qs["client_type"] == ["extension"]
    assert qs["callback_url"] == ["http://gw.example:8000/admin/oauth/callback"]
    assert qs["redirect_uri"] == ["http://gw.example:8000/admin/oauth/callback"]
    assert "cline|" in qs["state"][0]
    assert state == c._pending_sessions["cline"]
    assert verifier is None


# ── device_poll：state POST + authUrl ──────────────────────

@pytest.mark.asyncio
async def test_start_device_poll_posts_state_and_parses_authurl(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(
        calls, [(200, {"code": 0, "data": {"state": "S-42", "authUrl": "https://www.codebuddy.ai/auth?state=S-42"}})]))
    poll_started = {}

    async def fake_poll(provider_code, state, owner, interval):
        poll_started["args"] = (provider_code, state, owner, interval)

    c._poll_codebuddy_token = fake_poll
    r = await c.start_device_poll("codebuddy_intl", None)
    import asyncio
    await asyncio.sleep(0.05)  # 让后台轮询 task 得到调度
    post = calls[0]
    assert post["method"] == "post"
    assert post["url"].endswith("/v2/plugin/auth/state?platform=ide")
    assert post["content"] == "{}"
    assert post["headers"]["X-Domain"] == "www.codebuddy.ai"
    assert post["headers"]["X-No-Authorization"] == "true"
    assert r["login_url"] == "https://www.codebuddy.ai/auth?state=S-42"
    assert r["state"] == "S-42"
    assert poll_started["args"][0] == "codebuddy_intl" and poll_started["args"][1] == "S-42"


@pytest.mark.asyncio
async def test_poll_token_handles_pending_11217_then_success(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    # 第一次 pending(11217)，第二次成功(code 0 + accessToken)
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"code": 11217, "msg": "RetryFetchToken"}),
        (200, {"code": 0, "data": {"accessToken": "AT", "refreshToken": "RT",
                                    "tokenType": "Bearer", "expiresIn": 7200}}),
    ]))
    saved = {}

    async def fake_save(db, code, owner, tok, update_existing=None):
        saved["args"] = (code, owner, tok)

    c._save_token = fake_save
    monkeypatch.setattr(oc, "AsyncSessionLocal",
                        _fake_sessionmaker())
    await c._poll_codebuddy_token("codebuddy_cn", "S-1", "__default", 1)  # 1ms 间隔
    assert saved["args"][0] == "codebuddy_cn"
    assert saved["args"][2]["access_token"] == "AT"
    assert saved["args"][2]["expires_in"] == 7200
    # 两次 GET 轮询，且带 X-Domain / X-No-* 完整匿名头
    assert len(calls) == 2
    assert calls[0]["headers"]["X-Domain"] == "copilot.tencent.com"
    assert calls[0]["headers"]["X-No-Department-Info"] == "true"


# ── refresh 分发 + cline JSON 刷新 ─────────────────────────

@pytest.mark.asyncio
async def test_refresh_dispatch_by_style(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    hits = []

    async def fake_cb(db, provider, existing, rt):
        hits.append("codebuddy"); return True, "cb"

    async def fake_cl(db, provider, existing, rt):
        hits.append("cline"); return True, "cl"

    async def fake_get_token(db, code, owner):
        return SimpleNamespace(refresh_token_enc="enc:RT", owner=owner,
                               last_error="", is_active=True)

    c._refresh_codebuddy = fake_cb
    c._refresh_cline = fake_cl
    c._get_token_record = fake_get_token
    db = SimpleNamespace(commit=lambda: None)
    assert await c._do_refresh(db, "codebuddy_intl", "__default") == (True, "cb")
    assert await c._do_refresh(db, "codebuddy_cn", "__default") == (True, "cb")
    assert await c._do_refresh(db, "cline", "__default") == (True, "cl")


@pytest.mark.asyncio
async def test_refresh_cline_flow(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(
        calls, [(200, {"success": True, "data": {"accessToken": "AT2",
                                                  "refreshToken": "RT2", "expiresAt": exp}})]))
    saved = {}

    async def fake_save(db, code, owner, tok, update_existing=None):
        saved.update(code=code, owner=owner, tok=tok)

    c._save_token = fake_save
    existing = SimpleNamespace(refresh_token_enc="enc:RT", owner="__default",
                               last_error="", is_active=True)

    class _DB:
        async def commit(self): pass

    ok, msg = await c._refresh_cline(_DB(), get_oauth_provider("cline"), existing, "RT")
    assert ok, msg
    body = calls[0]["json"]
    assert body == {"refreshToken": "RT", "grantType": "refresh_token", "clientType": "extension"}
    assert saved["tok"]["access_token"] == "AT2"
    assert saved["tok"]["refresh_token"] == "RT2"
    assert 3500 < saved["tok"]["expires_in"] <= 3660 + 5


# ── cline 无 state 收尾（挂起会话） ────────────────────────

@pytest.mark.asyncio
async def test_complete_pending_stateless(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    import base64
    tok_json = {"accessToken": "eyJX.Y.z", "refreshToken": "rt"}
    code = base64.b64encode(json.dumps(tok_json).encode()).decode()
    # authorize 时登记的挂起会话（回调没带回 state）
    c._pending_sessions["cline"] = "cline|__default|rand"
    saved = {}

    async def fake_save(db, code_, owner, tok, update_existing=None):
        saved.update(code=code_, owner=owner, tok=tok)
        return SimpleNamespace(id=1)

    c._save_token = fake_save
    ok, msg, saved_tok = await c.complete_pending(code, _DB())
    assert ok, msg
    assert saved["code"] == "cline"
    assert saved["tok"]["access_token"] == "eyJX.Y.z"
    assert "cline" not in c._pending_sessions  # 收尾后清掉


class _DB:
    async def commit(self):
        pass


def _fake_sessionmaker():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield _DB()
    return _cm


# ── u1s1（有一说一）设备登录 ──────────────────────────────

def test_u1s1_registry_entry():
    p = get_oauth_provider("u1s1")
    assert p is not None
    ep = p.extra_params or {}
    assert ep.get("auth_mode") == "u1s1_device"
    assert ep.get("refresh_style") == "none"
    assert ep["device_start_url"] == "https://api.u1s1.io/auth/device/start"
    assert ep["device_poll_url"] == "https://api.u1s1.io/auth/device/poll"
    assert p.api_base_url == "https://api.u1s1.io/v1"


@pytest.mark.asyncio
async def test_start_u1s1_device_sends_valid_p256_jwk(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"verify_url": "https://u1s1.io/d/abc", "poll_secret": "ps-1",
               "interval": 2, "expires_in": 900}),
    ]))
    started = {}

    async def fake_poll(provider_code, poll_secret, owner, interval, expires_in):
        started["args"] = (provider_code, poll_secret, owner, interval, expires_in)

    c._poll_u1s1_device = fake_poll
    r = await c.start_u1s1_device("u1s1", None)
    import asyncio
    await asyncio.sleep(0.05)
    assert r["login_url"] == "https://u1s1.io/d/abc"
    assert r["state"] == "ps-1"
    body = calls[0]["json"]
    jwk = body["public_jwk"]
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256"
    # P-256 坐标 = 32 字节 → base64url 43 字符（无填充）
    assert len(jwk["x"]) == 43 and len(jwk["y"]) == 43
    assert body["device_name"] == "AIGate Gateway"
    assert started["args"] == ("u1s1", "ps-1", "__default", 2, 900)


@pytest.mark.asyncio
async def test_u1s1_start_error_bubbles_server_message(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (400, {"error": {"message": "invalid P-256 device public key"}}),
    ]))
    r = await c.start_u1s1_device("u1s1", None)
    assert "error" in r and "P-256" in r["error"]


@pytest.mark.asyncio
async def test_u1s1_poll_ok_saves_api_key(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"status": "ok", "api_key": "u1s1-real-key-abc",
               "device_token": "u1s1d-dev-1", "device_id": 7}),
    ]))
    monkeypatch.setattr(oc, "AsyncSessionLocal", _fake_sessionmaker())
    saved = {}

    async def fake_save(db, code, owner, tok, update_existing=None):
        saved.update(code=code, owner=owner, tok=tok)

    c._save_token = fake_save
    await c._poll_u1s1_device("u1s1", "ps-1", "__default", 1, 30)
    assert saved["code"] == "u1s1"
    assert saved["tok"]["access_token"] == "u1s1-real-key-abc"
    assert saved["tok"]["refresh_token"] == "u1s1d-dev-1"
    # 长期凭证：30 天过期 + refresh_style none 不会被调度器刷坏
    assert saved["tok"]["expires_in"] == 30 * 86400


@pytest.mark.asyncio
async def test_u1s1_poll_expired_stops(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"status": "expired"}),
    ]))
    saved = []

    async def fake_save(db, code, owner, tok, update_existing=None):
        saved.append(tok)

    c._save_token = fake_save
    await c._poll_u1s1_device("u1s1", "ps-1", "__default", 1, 30)
    assert saved == [] and len(calls) == 1


@pytest.mark.asyncio
async def test_refresh_style_none_short_circuits(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())

    async def fake_get_token(db, code, owner):
        return SimpleNamespace(refresh_token_enc="enc:RT", owner=owner,
                               last_error="", is_active=True)

    c._get_token_record = fake_get_token

    class _DB:
        async def commit(self): pass

    ok, msg = await c._do_refresh(_DB(), "u1s1", "__default")
    assert ok is True and "long-lived" in msg


def test_u1s1_headers_applied_by_adapter():
    from server.adapters.openai_compat import OpenAICompatAdapter
    a = OpenAICompatAdapter()
    h = a._get_headers("u1s1-key-123", {"__oauth": True}, "https://api.u1s1.io/v1")
    assert h["Authorization"] == "Bearer u1s1-key-123"   # 无前缀变换
    assert "__oauth" not in h
    assert h["x-u1s1-client"] == "terminal" and h["x-u1s1-version"]

def test_quirks_domain_matching():
    assert quirks_for("https://copilot.tencent.com/v2/chat/completions").name == "codebuddy_cn"
    assert quirks_for("https://www.codebuddy.ai/v2/chat/completions").name == "codebuddy_intl"
    assert quirks_for("https://api.cline.bot/api/v1/chat/completions").name == "cline"
    assert quirks_for("https://api.deepseek.com/v1") is None
    assert quirks_for(None) is None


def test_auth_token_workos_prefix():
    cline = quirks_for("https://api.cline.bot/api/v1/chat/completions")
    assert auth_token_for(cline, "eyJhbGciOiJSUzI1NiJ9.abc") == "workos:eyJhbGciOiJSUzI1NiJ9.abc"
    # 非 JWT（clp_ API key / 已前缀）原样
    assert auth_token_for(cline, "clp_xxx") == "clp_xxx"
    assert auth_token_for(cline, "workos:eyJx.y") == "workos:eyJx.y"
    # 其他域名不动
    cb = quirks_for("https://copilot.tencent.com/v2/chat/completions")
    assert auth_token_for(cb, "eyJx.y") == "eyJx.y"


def _payload(**kw):
    p = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    p.update(kw)
    return p


def test_transform_codebuddy_cn():
    q = quirks_for("https://copilot.tencent.com/v2/chat/completions")
    p = transform_payload(_payload(), q)
    assert p["stream"] is True  # 强制流式
    # agent system prompt 换中性
    agent = _payload(messages=[
        {"role": "system", "content": "You are Claude Code, Anthropic's official CLI for Claude."},
        {"role": "user", "content": "hi"}])
    p2 = transform_payload(agent, q)
    assert "helpful AI assistant" in p2["messages"][0]["content"]
    # 超长 system（>2000 字）同样替换；短用户 system 保留
    short = _payload(messages=[{"role": "system", "content": "你是翻译。"},
                               {"role": "user", "content": "hi"}])
    assert transform_payload(short, q)["messages"][0]["content"] == "你是翻译。"
    # reasoning_effort：none/off 删除；有 effort 补 reasoning_summary
    assert "reasoning_effort" not in transform_payload(_payload(reasoning_effort="none"), q)
    assert transform_payload(_payload(reasoning_effort="high"), q)["reasoning_summary"] == "auto"
    # user string 不被转 typed（CN 保持原形态）
    assert p["messages"][0]["content"] == "hi"


def test_transform_codebuddy_intl():
    q = quirks_for("https://www.codebuddy.ai/v2/chat/completions")
    p = transform_payload(_payload(messages=[
        {"role": "system", "content": "whatever system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "yo"},
        {"role": "user", "content": [{"type": "text", "text": "x"}], }]), q)
    assert p["stream"] is True
    assert p["messages"][0] == {"role": "system", "content": "You are CodeBuddy Code."}
    assert p["messages"][1]["content"] == [{"type": "text", "text": "hello"}]
    assert p["messages"][2] == {"role": "assistant", "content": "yo"}
    assert p["messages"][3]["content"] == [{"type": "text", "text": "x"}]
    # 原 system 被丢弃，只留固定自家 system
    assert sum(1 for m in p["messages"] if m.get("role") == "system") == 1


def test_unwrap_response():
    q = quirks_for("https://api.cline.bot/api/v1/chat/completions")
    env = {"success": True, "data": {"choices": [{"message": {"content": "ok"}}]}}
    assert unwrap_response(env, q)["choices"][0]["message"]["content"] == "ok"
    err = {"success": False, "error": "x"}
    assert unwrap_response(err, q) is err
    # 非 Cline 域名不碰
    assert unwrap_response(env, None) is env


# ── adapter 头构建 ─────────────────────────────────────────

def test_get_headers_oauth_marker_stripped_and_quirks_applied():
    from server.adapters.openai_compat import OpenAICompatAdapter
    a = OpenAICompatAdapter()
    h = a._get_headers("eyJx.y.z",
                       {"__oauth": True, "__proxy_force": True, "X-Own": "1"},
                       "https://api.cline.bot/api/v1/chat/completions")
    assert h["Authorization"] == "Bearer workos:eyJx.y.z"
    assert "__oauth" not in h and "__proxy_force" not in h
    assert h["X-Own"] == "1" and h["HTTP-Referer"] == "https://cline.bot"
    # provider 自定义头覆盖方言默认头
    h2 = a._get_headers("K", {"User-Agent": "MINE"},
                        "https://copilot.tencent.com/v2/chat/completions")
    assert h2["User-Agent"] == "MINE" and h2["x-codebuddy-request"] == "1"


# ── 流式专属上游：非流式调用自动聚合 ───────────────────────

class _StreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def make_fake_stream_client(calls, lines, status=200):
    class _Resp:
        status_code = status
        request = None
        reason_phrase = "OK"
        text = ""

        async def aiter_lines(self):
            for l in lines:
                yield l

        async def aread(self):
            return b"upstream error body"

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "json": json})
            return _StreamCtx(_Resp())

    return _Client


@pytest.mark.asyncio
async def test_chat_completion_codebuddy_aggregates_stream(monkeypatch):
    from server.adapters.openai_compat import OpenAICompatAdapter
    import server.adapters.openai_compat as oa
    a = OpenAICompatAdapter()
    calls = []
    lines = [
        'data: {"id":"c1","created":9,"model":"glm-5.3","choices":[{"index":0,"delta":{"role":"assistant","content":"He"}}]}',
        'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"llo"}}]}',
        'data: {"id":"c1","choices":[{"index":0,"delta":{"reasoning_content":"think"}}]}',
        'data: {"id":"c1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"get_"}}]}}]}',
        'data: {"id":"c1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"time","arguments":"{\\"a\\":1}"}}]},"finish_reason":"tool_calls"}]}',
        'data: {"id":"c1","usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12}}',
        'data: [DONE]',
    ]
    monkeypatch.setattr(oa.httpx, "AsyncClient", make_fake_stream_client(calls, lines))
    from server.schemas.chat import ChatCompletionRequest
    req = ChatCompletionRequest.model_validate(
        {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}], "stream": False})
    data = await a.chat_completion(req, "tok", "https://copilot.tencent.com/v2/chat/completions")
    # 走了流式聚合而不是普通 POST
    assert len(calls) == 1
    sent = calls[0]["json"]
    assert sent["stream"] is True
    msg = data["choices"][0]["message"]
    assert msg["content"] == "Hello"
    assert msg["tool_calls"][0]["function"]["name"] == "get_time"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"a":1}'
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    assert data["usage"]["total_tokens"] == 12


@pytest.mark.asyncio
async def test_stream_chat_completion_applies_intl_shape(monkeypatch):
    from server.adapters.openai_compat import OpenAICompatAdapter
    import server.adapters.openai_compat as oa
    a = OpenAICompatAdapter()
    calls = []
    monkeypatch.setattr(oa.httpx, "AsyncClient", make_fake_stream_client(calls, ["data: [DONE]"]))
    from server.schemas.chat import ChatCompletionRequest
    req = ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    chunks = [c async for c in a.stream_chat_completion(
        req, "tok", "https://www.codebuddy.ai/v2/chat/completions")]
    assert chunks == []
    sent = calls[0]["json"]
    assert sent["messages"][0] == {"role": "system", "content": "You are CodeBuddy Code."}
    assert sent["messages"][1]["content"] == [{"type": "text", "text": "hi"}]
    assert calls[0]["headers"]["X-IDE-Type"] == "IDE"
