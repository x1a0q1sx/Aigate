"""Qoder 移植测试：WAF 编码、COSY 签名一致性、消息规范化、payload 构造、
SSE 信封解包/合流、设备流轮询、自动建服务商、适配器注册。"""
import asyncio
import base64
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

import server.core.qoder_sign as qs
from server.core.qoder_sign import (
    qoder_encode_body, qoder_decode_body, build_cosy_headers,
)
from server.core.oauth_registry import get_oauth_provider
from server.adapters import qoder_adapter as qa_mod
from server.adapters.qoder_adapter import (
    QoderAdapter, _normalize_messages, _context_tiers, _is_billing_block,
)


# ── 编码 ────────────────────────────────────────────
def test_encode_decode_roundtrip():
    for payload in (b"hello qoder", "中文与 emoji 🚀".encode(), bytes(range(256))):
        enc = qoder_encode_body(payload)
        assert qoder_decode_body(enc) == payload
        # 编码产物不含标准 base64 特征字符映射前的原文
        assert enc != base64.b64encode(payload)


def test_encode_alphabet_substitution():
    enc = qoder_encode_body(b'{"a":1}')
    s = enc.decode("latin-1")
    assert any(ch in "_@,#$&*^()." for ch in s)  # 自定义字母表字符出现


# ── COSY 签名自洽 ───────────────────────────────────
def test_cosy_headers_signature_consistency():
    body = b'{"prompt":"hi"}'
    url = "https://api3.qoder.sh/algo/api/v2/model/list"
    creds = {"user_id": "u1", "auth_token": "dt-abc", "name": "n", "email": "e@x",
             "machine_id": "m-1"}
    h = build_cosy_headers(body, url, creds)
    auth = h["Authorization"]
    assert auth.startswith("Bearer COSY.")
    payload_b64, sig = auth[len("Bearer COSY."):].rsplit(".", 1)
    assert re_full_md5(sig)
    assert h["Cosy-User"] == "u1"
    assert h["Cosy-Sigpath"] == "/api/v2/model/list"  # /algo 前缀剥离
    assert h["Cosy-Bodyhash"] == hashlib.md5(body).hexdigest()
    assert h["Cosy-Bodylength"] == str(len(body))
    assert h["Cosy-Machineid"] == "m-1" and h["Cosy-Machinetoken"] == "m-1"
    # 重新计算签名：payload/cosyKey/ts/body/sigPath 五段拼接
    recomputed = hashlib.md5("\n".join(
        [payload_b64, h["Cosy-Key"], h["Cosy-Date"],
         body.decode("latin-1"), h["Cosy-Sigpath"]]).encode("latin-1")).hexdigest()
    assert recomputed == sig
    # info 字段可被同 key 解出（AES key = uuid 前 16 字符，iv=key）——结构校验
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    assert len(h["Cosy-Key"]) > 100  # RSA-1024 密文 base64


def re_full_md5(s):
    import re
    return re.fullmatch(r"[0-9a-f]{32}", s) is not None


def test_cosy_requires_identity():
    with pytest.raises(ValueError):
        build_cosy_headers(b"", "https://x", {"user_id": "", "auth_token": "t"})
    with pytest.raises(ValueError):
        build_cosy_headers(b"", "https://x", {"user_id": "u", "auth_token": ""})


# ── 消息规范化 ──────────────────────────────────────
def test_normalize_hoists_system_and_flattens():
    msgs = [
        {"role": "system", "content": "You are Qoder."},
        {"role": "user", "content": [{"type": "text", "text": "看"}, {"type": "text", "text": "图"}]},
        {"role": "assistant", "content": "ok", "tool_calls": [{"id": "t1"}]},
    ]
    out, system = _normalize_messages(msgs)
    assert system == "You are Qoder."
    assert all(m["role"] != "system" for m in out)
    assert out[0]["content"] == "看\n图"
    assert out[1].get("tool_calls") == [{"id": "t1"}]


def test_normalize_keeps_images_and_stubs_huge():
    big = "data:image/png;base64," + "A" * (600 * 1024)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "x"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": big}},
    ]}]
    out, _ = _normalize_messages(msgs)
    content = out[0]["content"]
    assert isinstance(content, list)
    assert any(b.get("type") == "image_url" for b in content)
    assert any("image attached" in str(b.get("text", "")) for b in content)


# ── payload 构造 ────────────────────────────────────
def _cfg(**over):
    base = {"key": "ultimate", "max_output_tokens": 32000, "max_input_tokens": 180000,
            "is_reasoning": False, "source": "system",
            "context_config": [{"name": "200K", "token_count": 200000, "is_default": True},
                               {"name": "1M", "token_count": 1000000}]}
    base.update(over)
    return base


def test_build_payload_shape_and_ids():
    a = QoderAdapter()
    creds = {"user_id": "u1", "auth_token": "dt-1", "machine_id": "m"}
    body = {"model": "ultimate",
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "你好呀"}],
            "tools": [{"type": "function", "function": {"name": "f1"}}]}
    p1 = a._build_payload(body, "ultimate", _cfg(), creds)
    assert p1["stream"] is True and p1["session_type"] == "qodercli"
    assert p1["system"] == "sys" and p1["chat_context"]["text"] == "你好呀"
    assert p1["parameters"]["max_tokens"] == 32000
    assert p1["chat_context"]["extra"]["modelConfig"] == {"key": "ultimate", "is_reasoning": False}
    p2 = a._build_payload(body, "ultimate", _cfg(), creds)
    assert p1["session_id"] == p2["session_id"]          # 稳定会话/记录 id
    assert p1["chat_record_id"] == p2["chat_record_id"]
    assert p1["request_id"] != p2["request_id"]           # per-call id


def test_build_payload_context_tier_auto_upgrade():
    a = QoderAdapter()
    long_text = "汉" * 500_000  # 估算远超 200K → 升 1M
    body = {"model": "ultimate", "messages": [{"role": "user", "content": long_text}]}
    p = a._build_payload(body, "ultimate", _cfg(), {"user_id": "u"})
    assert p["parameters"]["context_length"] == 1_000_000
    assert p["model_config"]["max_input_tokens"] == 1_000_000
    assert p["chat_context"]["extra"]["ideModelConfigOverride"]["max_input_tokens"] == 1_000_000


def test_build_payload_max_tokens_min_rule():
    a = QoderAdapter()
    body = {"model": "lite", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1000}
    p = a._build_payload(body, "lite", _cfg(max_output_tokens=8000), {"user_id": "u"})
    assert p["parameters"]["max_tokens"] == 1000


# ── SSE 合流 ────────────────────────────────────────
def _env(chunk):
    return json.dumps({"statusCodeValue": 200,
                       "body": json.dumps(chunk, ensure_ascii=False)}, ensure_ascii=False)


def test_coalescer_finish_usage_merge():
    co = QoderAdapter._Coalescer("qoder/ultimate")
    out = co.handle(json.dumps({"id": "c1", "choices": [
        {"index": 0, "delta": {"content": "Hello"}}]}))
    assert len(out) == 1 and out[0]["choices"][0]["delta"]["content"] == "Hello"
    out = co.handle(json.dumps({"id": "c1", "choices": [
        {"index": 0, "delta": {"content": "!", "finish_reason": "stop"}}]}))
    assert len(out) == 1
    out = co.handle(json.dumps({"choices": [],
                                "usage": {"prompt_tokens": 5, "completion_tokens": 2}}))
    assert out == []  # finish 已转发过，终态合流等流尾
    tail = co.flush()
    assert len(tail) == 1
    term = tail[0]
    assert term["choices"][0]["finish_reason"] == "stop"
    assert term["usage"]["prompt_tokens"] == 5


def test_is_billing_block():
    assert _is_billing_block('{"code":"112","message":"quota"}')
    assert _is_billing_block('{"pricingUrl":"https://x","e":1}')
    assert not _is_billing_block("normal error")


# ── 适配器流式端到端（fake httpx stream）────────────
class _FakeStreamResp:
    def __init__(self, lines, status=200):
        self.status_code = status
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return b"err-body"

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


def _patch_stream(monkeypatch, lines, status=200):
    recorded = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, content=None):
            recorded["method"] = method
            recorded["url"] = url
            recorded["headers"] = headers or {}
            recorded["content"] = content
            return _FakeStreamResp(lines, status)

    monkeypatch.setattr(qa_mod.httpx, "AsyncClient", _Client)
    return recorded


def _ready_adapter(monkeypatch):
    a = QoderAdapter()

    async def fake_resolve(api_key):
        return "dt-1", {"user_id": "u1", "auth_token": "dt-1",
                        "name": "", "email": "", "machine_id": "m"}

    async def fake_cfg(creds, key):
        return _cfg(key=key)

    monkeypatch.setattr(a, "_resolve_credentials", fake_resolve)
    monkeypatch.setattr(a, "_model_config", fake_cfg)
    return a


def test_stream_unwraps_envelope_and_signs(monkeypatch):
    a = _ready_adapter(monkeypatch)
    lines = [
        "data: " + _env({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        "data: " + _env({"choices": [{"index": 0, "delta": {"content": "你好"}}]}),
        "data: " + _env({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
    ]
    rec = _patch_stream(monkeypatch, lines)

    async def run():
        req = SimpleNamespace(model="qoder/ultimate",
                              model_dump=lambda: {"model": "qoder/ultimate",
                                                  "messages": [{"role": "user", "content": "hi"}]})
        return [c async for c in a.stream_chat_completion(req, "dt-1", "")]

    chunks = asyncio.run(run())
    assert rec["url"].endswith("&Encode=1")
    assert "ultimate" == json.loads(qoder_decode_body(rec["content"]))["chat_context"]["extra"]["modelConfig"]["key"]
    assert rec["headers"]["Authorization"].startswith("Bearer COSY.")
    assert rec["headers"]["X-Model-Key"] == "ultimate"
    texts = [c["choices"][0]["delta"].get("content") for c in chunks if c.get("choices")]
    assert "你好" in texts


def test_stream_billing_first_frame_raises(monkeypatch):
    a = _ready_adapter(monkeypatch)
    lines = ["data: " + json.dumps({"statusCodeValue": 403,
                                    "body": '{"code":"112","message":"quota exhausted"}'})]
    _patch_stream(monkeypatch, lines)

    async def run():
        req = SimpleNamespace(model="lite",
                              model_dump=lambda: {"model": "lite",
                                                  "messages": [{"role": "user", "content": "hi"}]})
        async for _ in a.stream_chat_completion(req, "dt-1", ""):
            pass

    with pytest.raises(RuntimeError, match="112|计费"):
        asyncio.run(run())


def test_non_stream_collects_tools_and_usage(monkeypatch):
    a = _ready_adapter(monkeypatch)
    lines = [
        "data: " + _env({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "get_", "arguments": ""}}]}}]}),
        "data: " + _env({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"name": "time", "arguments": '{"a":1}'}}]}}]}),
        "data: " + _env({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
        "data: " + _env({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}),
    ]
    _patch_stream(monkeypatch, lines)

    async def run():
        req = SimpleNamespace(model="lite",
                              model_dump=lambda: {"model": "lite",
                                                  "messages": [{"role": "user", "content": "time?"}]})
        return await a.chat_completion(req, "dt-1", "")

    out = asyncio.run(run())
    msg = out["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "get_time"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"a":1}'
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"]["prompt_tokens"] == 7


# ── 设备流 ──────────────────────────────────────────
def test_qoder_registry_profile():
    p = get_oauth_provider("qoder")
    assert (p.extra_params or {}).get("auth_mode") == "qoder_device"
    assert p.extra_params["refresh_style"] == "none"
    assert p.adapter_api_type == "qoder"
    assert p.token_url == "https://openapi.qoder.sh/api/v1/deviceToken/poll"
    ids = [m["model_id"] for m in p.static_models]
    assert "auto" in ids and "qmodel_38max" in ids


def test_qoder_poll_flow(monkeypatch):
    import server.core.oauth_client as oc

    calls = []
    results = [
        (202, {}),                                                    # 等待批准
        (200, {"token": "dt-new", "user_id": "u42",
               "expires_at": "2999-01-01T00:00:00Z", "refresh_token": "rt"}),
        (200, {"name": "user42", "email": "u@qoder.sh", "organization_id": "o1"}),
    ]

    class _Resp:
        def __init__(self, status, data):
            self.status_code = status
            self._data = data
            self.text = json.dumps(data)

        @property
        def is_success(self):
            return 200 <= self.status_code < 400

        def json(self):
            return self._data

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            calls.append(url.split("?")[0])
            s, d = results.pop(0) if results else (200, {})
            return _Resp(s, d)

    client = oc.OAuthClient()
    saved = {}

    async def fake_save(db, provider_code, owner, tok, update_existing=None):
        saved.update({"provider_code": provider_code, "owner": owner, "tok": tok})

    client._save_token = fake_save
    monkeypatch.setattr(oc.httpx, "AsyncClient", _Client)

    async def no_sleep(_):
        return

    monkeypatch.setattr(oc.asyncio, "sleep", no_sleep)
    asyncio.run(client._poll_qoder_device("qoder", "nonce", "verifier", "machine-1", "__default"))
    assert saved["provider_code"] == "qoder"
    assert saved["tok"]["access_token"] == "dt-new"
    meta = json.loads(saved["tok"]["scope"])
    assert meta["uid"] == "u42" and meta["email"] == "u@qoder.sh"
    assert meta["machine_id"] == "machine-1"
    assert saved["tok"]["expires_in"] > 365 * 86400 - 10  # 2999 年 → 触顶钳制为 30 天*
    # 30 天上限语义：expires_in = max(3600, expire-now) 不设上限，2999 应很大
    assert calls[0].endswith("deviceToken/poll")


def test_qoder_start_returns_login_url(monkeypatch):
    import server.core.oauth_client as oc
    client = oc.OAuthClient()
    started = {}

    async def fake_poll(*a, **k):
        started["poll"] = True

    monkeypatch.setattr(client, "_poll_qoder_device", fake_poll)
    r = asyncio.run(client.start_qoder_device("qoder", None))
    assert r["login_url"].startswith("https://qoder.com/device/selectAccounts?challenge=")
    assert "machine_id=" in r["login_url"] and "nonce=" in r["login_url"]
    assert r["state"] and r["poll_interval_ms"] == 2000


# ── 自动建服务商（sqlite）────────────────────────────
@pytest.mark.asyncio
async def test_save_token_auto_registers_provider(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from server.db import Base
    from server.models.oauth_token import OAuthToken
    from server.models.provider import Provider
    from sqlalchemy import select

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda s: Base.metadata.create_all(s, tables=[OAuthToken.__table__, Provider.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False)

    import server.core.oauth_client as oc
    client = oc.OAuthClient()
    client._crypto = SimpleNamespace(encrypt=lambda s: "enc:" + (s or ""),
                                     decrypt=lambda s: (s or "").removeprefix("enc:"))
    async with Session() as db:
        await client._save_token(db, "qoder", "__default",
                                 {"access_token": "dt-1", "expires_in": 100, "scope": "{}"})
        rows = (await db.execute(select(Provider))).scalars().all()
        assert len(rows) == 1
        prov = rows[0]
        assert prov.oauth_code == "qoder" and prov.credential_type == "oauth"
        assert prov.api_type == "qoder" and prov.base_url.startswith("https://api3.qoder.sh")
        # 幂等：再次保存不产生第二个服务商
        await client._save_token(db, "qoder", "__default",
                                 {"access_token": "dt-2", "expires_in": 100, "scope": "{}"})
        rows = (await db.execute(select(Provider))).scalars().all()
        assert len(rows) == 1
    await engine.dispose()
