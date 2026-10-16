"""Meta Muse Code（muse）OAuth 接入 + Responses wire 适配测试。

协议来源（逐行核对 TheStreamCode/opencode-muse-auth，MIT）：
  - POST auth.meta.com/oidc/device/authorization/  form client_id → 设备码
  - POST auth.meta.com/oidc/device/token/          form grant_type=device_code → access_token
    （authorization_pending=继续轮询；slow_down=间隔 +5s；其它错误=终止）
  - POST api.meta.ai/muse-code/key                 JSON {"onboard":true} → api_key "LLM|…"
  - 推理 = Bearer api_key 调 api.meta.ai/v1/responses（Responses wire，非 chat/completions）

同时锁住对 codex_responses 基类的重构回归（DEFAULT_INSTRUCTIONS / EFFORT_LEVELS /
BUILTIN_MODELS / _list_headers 抽取），以及 muse「重铸失败不下线、按错误分级退避」
的刷新语义（铸 key 端点激进限流，绝不能被 60s 调度器打穿）。
"""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import server.core.oauth_client as oc
from server.core.oauth_client import OAuthClient
from server.core.oauth_registry import get_oauth_provider
from server.core.model_catalog import create_adapter_for_provider
from server.schemas.chat import ChatCompletionRequest


class FakeCrypto:
    def encrypt(self, s):
        return "enc:" + (s or "")

    def decrypt(self, s):
        return (s or "").removeprefix("enc:")


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = json.dumps(self._json)
        self.reason_phrase = "OK"

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def make_fake_httpx(calls, results=None):
    """httpx.AsyncClient 替身：记录调用（含构造 kwargs）+ 按队列返回响应。"""
    results = list(results or [])

    class _Client:
        def __init__(self, *a, **k):
            calls.append({"_client_init": k})

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


def _fake_sessionmaker():
    from contextlib import asynccontextmanager

    class _DB:
        async def commit(self):
            pass

        async def execute(self, *a, **k):
            raise RuntimeError("empty")

        async def add(self, *a, **k):
            pass

        async def refresh(self, *a, **k):
            pass

    @asynccontextmanager
    async def _cm():
        yield _DB()
    return _cm


class FakePool:
    """代理池替身：记录 force，返回固定代理（muse 侧必须 force=True 才出网）。"""

    def __init__(self):
        self.calls = []

    def proxied_kwargs(self, *, force=False):
        self.calls.append(force)
        return {"proxy": "http://127.0.0.1:7890"} if force else {}


# ── 注册表 ────────────────────────────────────────────────

def test_muse_registry_entry():
    p = get_oauth_provider("muse")
    assert p is not None
    ep = p.extra_params or {}
    assert ep["auth_mode"] == "muse_device"
    assert ep["refresh_style"] == "muse"
    assert ep["device_url"] == "https://auth.meta.com/oidc/device/authorization/"
    assert ep["device_token_url"] == "https://auth.meta.com/oidc/device/token/"
    assert ep["key_url"] == "https://api.meta.ai/muse-code/key"
    assert p.api_base_url == "https://api.meta.ai/v1"
    assert p.adapter_api_type == "muse_responses"
    # Meta 出口按国别封锁 → 自动登记服务商时必须强制走代理池
    assert ep["force_proxy"] is True
    # 设备码流没有回调、也没有标准 refresh_token
    assert p.redirect_uri == "" and p.use_pkce is False


def test_muse_registry_seed_models_are_official_ids():
    p = get_oauth_provider("muse")
    ids = {m["model_id"] for m in (p.static_models or [])}
    assert {"muse-spark-1.3", "muse-spark-1.2", "muse-wonder-1.0"} <= ids


# ── 设备码 start ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_muse_device_returns_login_url_and_code(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"device_code": "dc-1", "user_code": "ABCD-1234",
               "verification_uri": "https://auth.meta.com/device",
               "verification_uri_complete": "https://auth.meta.com/device?code=ABCD-1234",
               "interval": 5, "expires_in": 600}),
    ]))
    pool = FakePool()
    monkeypatch.setattr(oc, "get_proxy_pool", lambda: pool, raising=False)
    import server.core.proxy_pool as pp
    monkeypatch.setattr(pp, "get_proxy_pool", lambda: pool)

    started = {}

    async def fake_poll(provider_code, device_code, owner, interval, expires_in):
        started["args"] = (provider_code, device_code, owner, interval, expires_in)

    c._poll_muse_device = fake_poll
    r = await c.start_muse_device("muse", None)
    await asyncio.sleep(0.05)

    # 优先给已内嵌 code 的 complete 链接（用户不用再手输）
    assert r["login_url"].endswith("?code=ABCD-1234")
    assert r["state"] == "dc-1"
    assert r["user_code"] == "ABCD-1234"
    assert r["poll_interval_ms"] == 5000
    post = [x for x in calls if x.get("method") == "post"][0]
    assert post["url"] == "https://auth.meta.com/oidc/device/authorization/"
    assert post["data"] == {"client_id": "1031625952748946"}
    assert post["headers"]["x-api-version"] == "1.0.0"
    assert started["args"] == ("muse", "dc-1", "__default", 5, 600)
    # OAuth 前置调用强制走代理池（服务端直连 Meta 不通）
    assert pool.calls and all(pool.calls)


@pytest.mark.asyncio
async def test_start_muse_device_rejects_wrong_auth_mode(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    r = await c.start_muse_device("u1s1", None)
    assert "error" in r


@pytest.mark.asyncio
async def test_start_muse_device_error_bubbles(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (400, {"error": "invalid_client"}),
    ]))
    r = await c.start_muse_device("muse", None)
    assert "error" in r and "invalid_client" in r["error"]


@pytest.mark.asyncio
async def test_start_muse_device_requires_all_fields(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"user_code": "X"}),   # 缺 device_code / verification_uri
    ]))
    r = await c.start_muse_device("muse", None)
    assert "error" in r and "missing fields" in r["error"]


@pytest.mark.asyncio
async def test_start_muse_device_retries_transport_failure(monkeypatch):
    """出口抖动（SSL EOF）重试一次即成功——生产实测用户点 5 次连接有 2 次撞上抖动。"""
    import asyncio as _real_asyncio
    import httpx as _httpx

    attempts = {"n": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, data=None, **k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _httpx.ConnectError(
                    "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
            return _Resp(200, {"device_code": "dc-2", "user_code": "AAAA-1111",
                               "verification_uri": "https://auth.meta.com/oauth/device/",
                               "interval": 5, "expires_in": 600})

    class _FastAsyncio:
        @staticmethod
        async def sleep(*a, **k):
            return None

        create_task = staticmethod(_real_asyncio.create_task)

    monkeypatch.setattr(oc.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(oc, "asyncio", _FastAsyncio)
    c = OAuthClient(crypto=FakeCrypto())

    async def fake_poll(*a, **k):
        pass

    c._poll_muse_device = fake_poll
    r = await c.start_muse_device("muse", None)
    assert r["state"] == "dc-2" and attempts["n"] == 2


@pytest.mark.asyncio
async def test_start_muse_device_http_400_not_retried(monkeypatch):
    """HTTP 4xx 是上游判定（设备码申请限流等），重试没意义 → 只发一次。"""
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (429, {"error": "slow_down"}),
    ]))
    r = await c.start_muse_device("muse", None)
    assert "error" in r and "429" in r["error"]
    assert len([x for x in calls if x.get("method") == "post"]) == 1


# ── 轮询 → 铸 key → 落库 ──────────────────────────────────

@pytest.mark.asyncio
async def test_poll_muse_device_pending_then_mints_and_saves(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"error": "authorization_pending"}),
        (200, {"access_token": "oauth-token-1"}),
        (200, {"api_key": "LLM|abc123", "user_id": "1001",
               "user_email": "Dev@Meta.Example", "is_subs_active": True}),
    ]))
    monkeypatch.setattr(oc, "AsyncSessionLocal", _fake_sessionmaker())
    saved = {}

    async def fake_get_token_record(db, provider_code, owner):
        return None

    async def fake_save(db, code, owner, tok, update_existing=None, allow_identity_change=False):
        saved.update(code=code, owner=owner, tok=tok)

    c._get_token_record = fake_get_token_record
    c._save_token = fake_save
    await c._poll_muse_device("muse", "dc-1", "__default", 1, 30)

    assert saved["code"] == "muse"
    # 推理凭证 = 铸出的 LLM| key；OAuth token 只作为重铸材料存 refresh 列
    assert saved["tok"]["access_token"] == "LLM|abc123"
    assert saved["tok"]["refresh_token"] == "oauth-token-1"
    meta = json.loads(saved["tok"]["scope"])
    assert meta == {"account_id": "1001", "email": "dev@meta.example"}
    # 新连接自动名 → 换成账号邮箱
    assert saved["owner"] == "dev@meta.example"
    posts = [x for x in calls if x.get("method") == "post"]
    assert posts[0]["data"]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert posts[0]["data"]["device_code"] == "dc-1"
    # 最后一次 POST = 铸 key
    assert posts[-1]["url"] == "https://api.meta.ai/muse-code/key"
    assert posts[-1]["json"] == {"onboard": True}
    assert posts[-1]["headers"]["Authorization"] == "Bearer oauth-token-1"


@pytest.mark.asyncio
async def test_poll_muse_device_terminal_error_stops(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"error": "expired_token"}),
    ]))
    saved = {}

    async def fake_finish(*a, **k):
        saved["called"] = True

    c._finish_muse_login = fake_finish
    await c._poll_muse_device("muse", "dc-1", "__default", 1, 30)
    assert not saved
    # 终态失败必须记录到设备流状态（前端 device-status 接口消费），且带上排障提示
    st = oc.get_device_flow_status("muse")
    assert st["status"] == "failed"
    assert "expired_token" in st["error"]
    assert "Sign-in failed" in st["error"]   # 浏览器侧被拒的排障指引


@pytest.mark.asyncio
async def test_poll_muse_device_success_records_ok_status(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"access_token": "oauth-token-1"}),
        (200, {"api_key": "LLM|abc", "user_id": "1", "user_email": "a@b.c"}),
    ]))
    monkeypatch.setattr(oc, "AsyncSessionLocal", _fake_sessionmaker())

    async def fake_get_token_record(db, provider_code, owner):
        return None

    async def fake_save(db, code, owner, tok, update_existing=None, allow_identity_change=False):
        pass

    c._get_token_record = fake_get_token_record
    c._save_token = fake_save
    await c._poll_muse_device("muse", "dc-1", "__default", 1, 30)
    st = oc.get_device_flow_status("muse")
    assert st["status"] == "ok"


@pytest.mark.asyncio
async def test_poll_muse_device_mint_failure_records_reason(monkeypatch):
    """浏览器批准成功但铸 key 失败（如订阅未激活）→ 状态记录真实原因。"""
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"access_token": "oauth-token-1"}),
        (200, {"is_subs_active": False}),
    ]))
    monkeypatch.setattr(oc, "AsyncSessionLocal", _fake_sessionmaker())
    await c._poll_muse_device("muse", "dc-1", "__default", 1, 30)
    st = oc.get_device_flow_status("muse")
    assert st["status"] == "failed"
    assert "铸造推理 key 失败" in st["error"]
    assert "subscription is inactive" in st["error"]


@pytest.mark.asyncio
async def test_poll_muse_device_timeout_records_reason(monkeypatch):
    """轮询到有效期结束仍未批准 → 状态记录超时（含浏览器侧排障指引）。"""
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, []))
    # deadline = time.time() + max(60, expires_in+30) 有 60s 下限：让 fake time
    # 每次调用推进 120s → 第 2 次（while 判定）即超期，无需真实 sleep。
    _t = {"now": 1000.0}

    def fake_time():
        v = _t["now"]
        _t["now"] += 120.0
        return v

    monkeypatch.setattr(oc.time, "time", fake_time)
    await c._poll_muse_device("muse", "dc-1", "__default", 1, 10)
    st = oc.get_device_flow_status("muse")
    assert st["status"] == "failed"
    assert "超时" in st["error"]
    assert "Sign-in failed" in st["error"]


@pytest.mark.asyncio
async def test_poll_muse_device_slow_down_extends_interval(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"error": "slow_down"}),
        (200, {"access_token": "oauth-token-1"}),
    ]))
    fin = {}

    async def fake_finish(provider_code, owner, access, ep, token_body=None, device_code=""):
        fin["access"] = access

    c._finish_muse_login = fake_finish
    await c._poll_muse_device("muse", "dc-1", "__default", 1, 30)
    assert fin.get("access") == "oauth-token-1"
    assert len([x for x in calls if x.get("method") == "post"]) == 2


@pytest.mark.asyncio
async def test_finish_muse_login_keeps_auto_name_when_email_taken(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"api_key": "LLM|x", "user_id": "1", "user_email": "a@b.c"}),
    ]))
    monkeypatch.setattr(oc, "AsyncSessionLocal", _fake_sessionmaker())
    saved = {}

    async def fake_get_token_record(db, provider_code, owner):
        # 邮箱那行已被占用 → 不得覆盖，保留自动名
        return SimpleNamespace(owner=owner) if owner == "a@b.c" else None

    async def fake_save(db, code, owner, tok, update_existing=None, allow_identity_change=False):
        saved["owner"] = owner

    c._get_token_record = fake_get_token_record
    c._save_token = fake_save
    await c._finish_muse_login("muse", "__default", "oauth-1",
                               get_oauth_provider("muse").extra_params)
    assert saved["owner"] == "__default"


# ── 铸 key 判据 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_mint_muse_key_inactive_subscription(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"is_subs_active": False}),
    ]))
    with pytest.raises(RuntimeError) as e:
        await c._mint_muse_key("oauth-1", {})
    assert "inactive" in str(e.value)


@pytest.mark.asyncio
async def test_mint_muse_key_requires_payment(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"require_payment": True, "require_payment_action_url": "https://meta.ai/billing"}),
    ]))
    with pytest.raises(RuntimeError) as e:
        await c._mint_muse_key("oauth-1", {})
    assert "subscription required" in str(e.value) and "billing" in str(e.value)


@pytest.mark.asyncio
async def test_mint_muse_key_http_error_carries_status(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (429, {"error": "rate limited"}),
    ]))
    with pytest.raises(RuntimeError) as e:
        await c._mint_muse_key("oauth-1", {})
    assert "429" in str(e.value)


# ── 刷新语义（重铸失败绝不下线 + 退避防打穿限流端点）──────

def _row():
    return SimpleNamespace(owner="dev@meta.example", is_active=True, last_error="",
                           expires_at=datetime.utcnow(), access_token_enc="enc:LLM|old",
                           refresh_token_enc="enc:oauth-1")


class _DB:
    def __init__(self):
        self.committed = 0

    async def commit(self):
        self.committed += 1


@pytest.mark.asyncio
async def test_refresh_muse_success_writes_new_key(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (200, {"api_key": "LLM|new", "user_id": "9", "user_email": "D@E.F"}),
    ]))
    saved = {}

    async def fake_save(db, code, owner, tok, update_existing=None, allow_identity_change=False):
        saved.update(owner=owner, tok=tok, upd=update_existing)

    c._save_token = fake_save
    db = _DB()
    row = _row()
    ok, msg = await c._refresh_muse(db, get_oauth_provider("muse"), row, "oauth-1")
    assert ok is True
    assert saved["tok"]["access_token"] == "LLM|new"
    # 重铸不换 OAuth token（同一授权材料可重复铸）
    assert saved["tok"]["refresh_token"] == "oauth-1"
    assert saved["upd"] is row and saved["owner"] == row.owner


@pytest.mark.asyncio
async def test_refresh_muse_401_defers_retry_and_stays_active(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (401, {"error": "revoked"}),
    ]))
    db = _DB()
    row = _row()
    before = row.expires_at
    ok, msg = await c._refresh_muse(db, get_oauth_provider("muse"), row, "oauth-1")
    assert ok is False
    # 关键：推理 key 可能仍有效 → 绝不下线连接
    assert row.is_active is True
    assert "重铸 key 失败" in row.last_error and "重新设备码登录" in row.last_error
    # 永久失效：推到 30 天，免得 60s 调度器把限流端点打穿
    assert row.expires_at - before >= timedelta(days=29)


@pytest.mark.asyncio
async def test_refresh_muse_429_defers_only_30_minutes(monkeypatch):
    c = OAuthClient(crypto=FakeCrypto())
    calls = []
    monkeypatch.setattr(oc.httpx, "AsyncClient", make_fake_httpx(calls, [
        (429, {"error": "rate limited"}),
    ]))
    db = _DB()
    row = _row()
    before = row.expires_at
    ok, msg = await c._refresh_muse(db, get_oauth_provider("muse"), row, "oauth-1")
    assert ok is False
    assert row.is_active is True
    assert timedelta(minutes=25) <= row.expires_at - before <= timedelta(minutes=35)


@pytest.mark.asyncio
async def test_do_refresh_dispatches_muse_style(monkeypatch):
    """refresh_style=muse 必须走重铸分支（而不是标准 grant_type=refresh_token）。"""
    c = OAuthClient(crypto=FakeCrypto())
    hit = {}

    async def fake_muse(db, provider, existing, refresh_plain):
        hit["token"] = refresh_plain
        return True, "reminted"

    async def fake_get_token_record(db, provider_code, owner):
        return _row()

    c._refresh_muse = fake_muse
    c._get_token_record = fake_get_token_record
    db = _DB()
    ok, msg = await c._do_refresh(db, "muse", "dev@meta.example")
    assert (ok, msg) == (True, "reminted")
    assert hit["token"] == "oauth-1"


# ── 适配器：Responses wire + Muse 指纹 ────────────────────

def _req(model="muse-spark-1.3", **kw):
    return ChatCompletionRequest(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        **kw,
    )


def test_factory_returns_muse_adapter():
    from server.adapters.muse_responses import MuseResponsesAdapter
    a = create_adapter_for_provider("muse_responses")
    assert isinstance(a, MuseResponsesAdapter)
    a2 = create_adapter_for_provider("muse_responses", timeout=33)
    assert a2.timeout == 33


def test_muse_headers_no_codex_fingerprint():
    from server.adapters.muse_responses import MuseResponsesAdapter
    h = MuseResponsesAdapter()._get_headers("LLM|k", _req(), {"__oauth": True, "__proxy_force": True})
    assert h["Authorization"] == "Bearer LLM|k"
    assert h["x-api-version"] == "1.0.0"
    # Codex 官方客户端指纹不得外溢到 Meta
    for k in ("originator", "X-Codex-Client", "OpenAI-Beta", "session_id"):
        assert k not in h
    # 内部标记（__oauth / __proxy_force）绝不出站
    assert not any(k.startswith("__") for k in h)


def test_muse_payload_omits_codex_instructions():
    from server.adapters.muse_responses import MuseResponsesAdapter
    p = MuseResponsesAdapter()._build_responses_payload(_req())
    assert "instructions" not in p
    # Responses wire 的核心形态不能退化
    assert p["model"] == "muse-spark-1.3"
    assert p["store"] is False
    assert p["reasoning"]["effort"] == "low"
    assert p["include"] == ["reasoning.encrypted_content"]


def test_muse_payload_keeps_user_system_message():
    from server.adapters.muse_responses import MuseResponsesAdapter
    req = ChatCompletionRequest(model="muse-spark-1.3", messages=[
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "hi"},
    ])
    p = MuseResponsesAdapter()._build_responses_payload(req)
    assert p["instructions"] == "Be terse."


def test_muse_effort_levels_include_minimal_and_max():
    from server.adapters.muse_responses import MuseResponsesAdapter
    a = MuseResponsesAdapter()
    assert a._build_responses_payload(_req(reasoning_effort="minimal"))["reasoning"]["effort"] == "minimal"
    assert a._build_responses_payload(_req("muse-spark-1.3-max"))["reasoning"]["effort"] == "max"
    assert a._build_responses_payload(_req("muse-spark-1.3-max"))["model"] == "muse-spark-1.3"
    # Codex 不认识的档位不能被塞给它（基类回归）
    from server.adapters.codex_responses import CodexResponsesAdapter
    cx = CodexResponsesAdapter()._build_responses_payload(_req("gpt-5.4", reasoning_effort="minimal"))
    assert cx["reasoning"]["effort"] == "low"


def test_muse_models_url_and_list_headers():
    from server.adapters.muse_responses import MuseResponsesAdapter
    a = MuseResponsesAdapter()
    assert a._build_models_url("https://api.meta.ai/v1") == "https://api.meta.ai/v1/models"
    assert a._build_url("https://api.meta.ai/v1") == "https://api.meta.ai/v1/responses"
    h = a._list_headers("LLM|k", None)
    assert h["x-api-version"] == "1.0.0" and "X-Codex-Client" not in h


def test_codex_adapter_regression_default_instructions_still_injected():
    """重构后 Codex 行为不变：无 system 时仍注入 Codex 文案，仍带官方客户端指纹。"""
    from server.adapters.codex_responses import CodexResponsesAdapter, CODEX_DEFAULT_INSTRUCTIONS
    req = ChatCompletionRequest(model="gpt-5.4", messages=[{"role": "user", "content": "hi"}])
    a = CodexResponsesAdapter()
    p = a._build_responses_payload(req)
    assert p["instructions"] == CODEX_DEFAULT_INSTRUCTIONS
    h = a._get_headers("sk-x", req, None)
    assert h["originator"] == "codex_cli_rs" and h["X-Codex-Client"] == "official"
    lh = a._list_headers("sk-x", None)
    assert lh["X-Codex-Client"] == "official" and lh["OpenAI-Beta"] == "responses=v1"


def test_muse_capabilities_reports_reasoning_effort_support():
    from server.core.model_capabilities import infer_reasoning_effort_support
    assert infer_reasoning_effort_support("muse_responses", "muse-spark-1.3") is True


# ── 自动登记服务商：必须带「走代理」开关 ──────────────────

@pytest.mark.asyncio
async def test_ensure_provider_registered_enables_proxy_for_muse(tmp_path):
    """注册表 extra_params.force_proxy=True → 服务商行建好即 proxy_enabled=True。

    这是 muse 能不能出网的分水岭：没有该开关就不会带 __proxy_force，
    推理/刷新直接打 api.meta.ai（服务端直连不通 → 全盘失败且不报配置错。
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from server.models.base import Base
    from server.models.provider import Provider

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/muse.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Provider.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    c = OAuthClient(crypto=FakeCrypto())
    async with Session() as db:
        await c._ensure_provider_registered(db, "muse")
        row = (await db.execute(
            select(Provider).where(Provider.oauth_code == "muse"))).scalar_one()
        assert row.api_type == "muse_responses"
        assert row.base_url == "https://api.meta.ai/v1"
        assert row.credential_type == "oauth"
        assert row.proxy_enabled is True
        # 幂等：再登记一次不新增行
        await c._ensure_provider_registered(db, "muse")
        rows = (await db.execute(
            select(Provider).where(Provider.oauth_code == "muse"))).scalars().all()
        assert len(rows) == 1
    await engine.dispose()
