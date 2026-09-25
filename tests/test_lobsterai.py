"""LobsterAI（有道龙虾）协议测试。

协议来源：Jet-Hub 源码逐行核对 + 2026-09 实测。测试锁死的每条都是
「反直觉、改错就出问题」的硬约束（见每用例 docstring）。
"""
import asyncio
import json

import pytest

import server.core.lobsterai as lb
from server.core.oauth_usage import _lobsterai_usage


def make_httpx(calls, results):
    """results: [(status, json_data)]；记录 GET/POST。"""
    results = list(results)

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

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _next(self):
            return _Resp(*results.pop(0)) if results else _Resp(200, {})

        async def get(self, url, headers=None, params=None):
            calls.append({"m": "GET", "url": url, "headers": headers or {},
                          "params": params or {}})
            return self._next()

        async def post(self, url, headers=None, json=None, content=None):
            calls.append({"m": "POST", "url": url, "headers": headers or {},
                          "json": json, "content": content})
            return self._next()

    return _Client


def _patch(monkeypatch, calls, results):
    monkeypatch.setattr(lb.httpx, "AsyncClient", make_httpx(calls, results))


# ── 版本号 ──────────────────────────────────────────
def test_parse_client_version_forms():
    assert lb.parse_client_version("2026.9.4") == "2026.9.4"
    assert lb.parse_client_version("2026.9.4-beta.1") == "2026.9.4-beta.1"
    assert lb.parse_client_version(" 2026.9.4 ") == "2026.9.4"
    # 非法形态必须拒绝：版本号是必填请求参数，拼进 URL 会以更费解的错误失败
    assert lb.parse_client_version("<html>") is None
    assert lb.parse_client_version("") is None
    assert lb.parse_client_version(None) is None
    assert lb.parse_client_version("v2026") is None


def test_parse_client_version_from_update_nested():
    """更新接口结构特殊：code/msg 在外层，载荷在 data.value.version。"""
    body = {"code": 0, "msg": "OK",
            "data": {"value": {"version": "2026.9.4", "date": "x"}}}
    assert lb.parse_client_version_from_update(body) == "2026.9.4"
    assert lb.parse_client_version_from_update({"data": {}}) is None
    assert lb.parse_client_version_from_update(None) is None


def test_resolve_client_version_falls_back(monkeypatch):
    """取不到版本号时回退兜底值（参考实现 sigin.py 是直接放弃，这里更宽容）。"""
    lb._VERSION_CACHE = (0.0, "")
    calls = []
    _patch(monkeypatch, calls, [(500, {})])
    v = asyncio.run(lb.resolve_client_version())
    assert v == lb.LOBSTERAI_FALLBACK_CLIENT_VERSION


# ── keyfrom 身份载荷 ────────────────────────────────
def test_keyfrom_body_field_rules():
    """uuid/userId 缺失时**删键**而非写空串（Go 的 if a.Uuid != '' 语义）。"""
    cred = {"first_keyfrom": "1700000000000", "latest_keyfrom": "1700000000001",
            "uuid": "u-1", "user_id": "uid-1"}
    body = lb.build_keyfrom_body(cred, "2026.9.4")
    assert body == {"firstKeyfrom": "1700000000000",
                    "latestKeyfrom": "1700000000001",
                    "version": "2026.9.4", "uuid": "u-1", "userId": "uid-1"}
    # 缺 uuid / userId
    body2 = lb.build_keyfrom_body({"first_keyfrom": "1", "latest_keyfrom": "2"}, "v9")
    assert "uuid" not in body2 and "userId" not in body2


# ── exchange ────────────────────────────────────────
def test_exchange_success_persists_identity(monkeypatch):
    """exchange body 必须含 5 字段；身份字段随凭据返回（续期必需）。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "code": 0, "msg": "OK",
        "data": {"accessToken": "at-1", "refreshToken": "rt-1", "expiresIn": 7200,
                 "user": {"id": "uid-1", "nickname": "小明"}},
    })])
    sess = {"uuid": "uuid-1", "first_keyfrom": "1700000000000"}
    cred, err = asyncio.run(lb.exchange_auth_code("code-1", sess, "2026.9.4"))
    assert err == ""
    assert cred["access_token"] == "at-1"
    assert cred["refresh_token"] == "rt-1"
    assert cred["expires_in"] == 7200
    assert cred["user_id"] == "uid-1"
    assert cred["uuid"] == "uuid-1"
    assert cred["first_keyfrom"] == "1700000000000"
    body = calls[0]["json"]
    assert set(body.keys()) == {"authCode", "firstKeyfrom", "latestKeyfrom", "uuid", "version"}
    assert body["authCode"] == "code-1"
    assert body["uuid"] == "uuid-1"
    # 换票端点不需要 Authorization
    assert "Authorization" not in calls[0]["headers"]


def test_exchange_envelope_error(monkeypatch):
    """信封 code != 0 必须失败（不能把错误响应当 token 存）。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {"code": 40100, "msg": "session 已失效", "data": {}})])
    cred, err = asyncio.run(lb.exchange_auth_code("bad", {"uuid": "u", "first_keyfrom": "1"}, "v"))
    assert cred is None
    assert "session 已失效" in err


def test_exchange_empty_data_is_failure(monkeypatch):
    """code=0 但 data 空 → 失败（sigin.py:46-47 的「accessToken 可能已失效」判据）。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {"code": 0, "msg": "OK", "data": {}})])
    cred, err = asyncio.run(lb.exchange_auth_code("c", {"uuid": "u", "first_keyfrom": "1"}, "v"))
    assert cred is None
    assert "data" in err


def test_exchange_non_json(monkeypatch):
    """HTML 错误页不能让 json() 异常冒穿。"""
    class _BadResp:
        status_code = 502
        content = b"<html>bad gateway</html>"
        text = "<html>bad gateway</html>"

        @property
        def is_success(self):
            return False

        def json(self):
            raise ValueError("not json")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _BadResp()

    monkeypatch.setattr(lb.httpx, "AsyncClient", _Client)
    cred, err = asyncio.run(lb.exchange_auth_code("c", {"uuid": "u", "first_keyfrom": "1"}, "v"))
    assert cred is None
    assert "不是 JSON" in err


# ── refresh ─────────────────────────────────────────
def test_refresh_body_includes_keyfrom(monkeypatch):
    """续期请求体 = keyfrom 载荷 + refreshToken（缺 keyfrom 上游必拒）。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "code": 0, "data": {"accessToken": "at-2", "refreshToken": "rt-2", "expiresIn": 3600},
    })])
    cred = {"refresh_token": "rt-1", "uuid": "uuid-1",
            "first_keyfrom": "1700000000000", "latest_keyfrom": "1700000000001",
            "client_version": "2026.9.4", "user_id": "uid-1"}
    new, err = asyncio.run(lb.refresh_token(cred))
    assert err == ""
    body = calls[0]["json"]
    assert body["refreshToken"] == "rt-1"
    assert body["firstKeyfrom"] == "1700000000000"
    assert body["uuid"] == "uuid-1"
    assert body["version"] == "2026.9.4"
    assert new["access_token"] == "at-2"


def test_refresh_latest_keyfrom_not_updated(monkeypatch):
    """latestKeyfrom 用**存储的原值**（字段名叫「最近活动」但刻意不更新）。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "code": 0, "data": {"accessToken": "at-2", "refreshToken": "rt-2", "expiresIn": 3600},
    })])
    cred = {"refresh_token": "rt-1", "uuid": "u", "first_keyfrom": "100",
            "latest_keyfrom": "200", "client_version": "v", "user_id": "uid"}
    new, _ = asyncio.run(lb.refresh_token(cred))
    assert calls[0]["json"]["latestKeyfrom"] == "200"     # 不是当前时刻
    assert new["latest_keyfrom"] == "200"                  # 回写也用旧值


def test_refresh_empty_new_token_keeps_old(monkeypatch):
    """refresh 响应可能不返回新 refreshToken → 不能覆盖成空串。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "code": 0, "data": {"accessToken": "at-2", "expiresIn": 3600},
    })])
    cred = {"refresh_token": "rt-old", "uuid": "u", "first_keyfrom": "1",
            "latest_keyfrom": "2", "client_version": "v", "user_id": "uid"}
    new, err = asyncio.run(lb.refresh_token(cred))
    assert err == ""
    assert new["refresh_token"] == "rt-old"


def test_refresh_no_refresh_token_stored():
    new, err = asyncio.run(lb.refresh_token({"uuid": "u", "first_keyfrom": "1"}))
    assert new is None
    assert "refresh_token" in err


def test_refresh_identity_fields_carried_over(monkeypatch):
    """身份字段一律沿用旧值（uuid/first_keyfrom/latest_keyfrom 都不新生成）。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {
        "code": 0, "data": {"accessToken": "at-2", "refreshToken": "rt-2", "expiresIn": 10},
    })])
    cred = {"refresh_token": "rt-1", "uuid": "keep-uuid", "first_keyfrom": "keep-first",
            "latest_keyfrom": "keep-latest", "client_version": "keep-ver", "user_id": "uid"}
    new, _ = asyncio.run(lb.refresh_token(cred))
    assert new["uuid"] == "keep-uuid"
    assert new["first_keyfrom"] == "keep-first"
    assert new["latest_keyfrom"] == "keep-latest"


# ── uid 四级回退 ────────────────────────────────────
def test_resolve_uid_four_levels():
    """user.id → user.userId → user.yid → sha256(token)[:16]（与 Go 逐字节一致）。"""
    import hashlib
    assert lb.resolve_uid({"user_id": "a", "account_user_id": "b", "yid": "c"}) == "a"
    assert lb.resolve_uid({"account_user_id": "b", "yid": "c"}) == "b"
    assert lb.resolve_uid({"yid": "c"}) == "c"
    tok = "tok-xyz"
    expect = hashlib.sha256(tok.encode()).hexdigest()[:16]
    assert lb.resolve_uid({"access_token": tok}) == expect


# ── 登录 URL ────────────────────────────────────────
def test_login_url_shape():
    """redirect_uri 必须是 127.0.0.1 形态（portal 会校验）+ 百分号编码。"""
    url = lb.build_login_url(18090, "st-1")
    assert url.startswith("https://lobsterai.youdao.com/portal#/login?")
    assert "source=electron" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A18090%2Fauth%2Fcallback" in url
    assert "state=st-1" in url


# ── 模型列表 ────────────────────────────────────────
def test_parse_models_envelope_flat_and_nested():
    body = {"code": 0, "data": [
        {"modelId": "m1", "modelName": "M1", "contextWindow": 1000000,
         "supportsImage": False, "maxTokens": 64000, "costMultiplier": 0.05},
    ]}
    out = lb.parse_models(body)
    assert len(out) == 1
    assert out[0]["id"] == "m1" and out[0]["name"] == "M1"
    assert out[0]["context_length"] == 1000000
    assert out[0]["supports_vision"] is False
    assert out[0]["max_output_tokens"] == 64000
    # 嵌套一层 data.data
    nested = {"code": 0, "data": {"data": [{"modelId": "m2"}]}}
    assert lb.parse_models(nested)[0]["id"] == "m2"
    # code != 0 → 空
    assert lb.parse_models({"code": 500, "data": [{"modelId": "m3"}]}) == []


def test_fetch_models_sends_capabilities_header(monkeypatch):
    """能力头是**准入条件**：不带时模型集合少 kimi-k3（实测 25 vs 26 个）。"""
    calls = []
    _patch(monkeypatch, calls, [(200, {"code": 0, "data": [{"modelId": "m1"}]})])
    cred = {"access_token": "at-1", "uuid": "u", "first_keyfrom": "1",
            "latest_keyfrom": "2", "client_version": "2026.9.4", "user_id": "uid"}
    out = asyncio.run(lb.fetch_models(cred))
    assert len(out) == 1
    h = calls[0]["headers"]
    assert h["X-LobsterAI-Client-Capabilities"] == "kimi-k3-agentic-v1,thinking-level-control-v1"
    assert h["X-LobsterAI-Client-Version"] == "2026.9.4"
    # query 是身份载荷
    assert calls[0]["params"]["firstKeyfrom"] == "1"
    assert calls[0]["params"]["uuid"] == "u"


# ── 额度查询 ────────────────────────────────────────
def test_lobsterai_usage_profile_summary(monkeypatch):
    """用 profile-summary 而非 quota（后者不含活动积分，实测 5297 vs 300）。"""
    import server.core.oauth_usage as ou
    calls = []
    monkeypatch.setattr(ou.httpx, "AsyncClient", make_httpx(calls, [(200, {
        "code": 0, "data": {"totalCreditsRemaining": 5297.72,
                            "creditItems": [{"type": "活动积分", "creditsRemaining": 5000},
                                            {"type": "免费积分", "creditsRemaining": 297.72}]},
    })]))
    r = asyncio.run(_lobsterai_usage("tok"))
    assert calls[0]["url"].endswith("/api/user/profile-summary")
    assert r["quotas"]["活动积分"]["remaining"] == 5000
    assert r["quotas"]["免费积分"]["remaining"] == 297.72
    assert r["extra"]["total_credits_remaining"] == 5297.72


def test_lobsterai_usage_negative_clamped(monkeypatch):
    """负值 clamp 到 0（超额扣费/计量回滚可能下发负值）。"""
    import server.core.oauth_usage as ou
    calls = []
    monkeypatch.setattr(ou.httpx, "AsyncClient", make_httpx(calls, [(200, {
        "code": 0, "data": {"totalCreditsRemaining": -12.5,
                            "creditItems": [{"type": "x", "creditsRemaining": -3}]},
    })]))
    r = asyncio.run(_lobsterai_usage("tok"))
    assert r["quotas"]["x"]["remaining"] == 0.0


def test_lobsterai_usage_nothing_is_not_zero(monkeypatch):
    """total=0 且无明细 → 报「查不到」而非「余额为 0」。"""
    import server.core.oauth_usage as ou
    calls = []
    monkeypatch.setattr(ou.httpx, "AsyncClient", make_httpx(calls, [(200, {
        "code": 0, "data": {},
    })]))
    r = asyncio.run(_lobsterai_usage("tok"))
    assert r["quotas"] == {}
    assert "无任何条目" in r["message"]


def test_lobsterai_usage_http_error(monkeypatch):
    import server.core.oauth_usage as ou
    calls = []
    monkeypatch.setattr(ou.httpx, "AsyncClient", make_httpx(calls, [(401, {})]))
    r = asyncio.run(_lobsterai_usage("tok"))
    assert "401" in r["message"]


def test_lobsterai_usage_business_error_in_200(monkeypatch):
    """HTTP 200 也可能带业务错误（实测无凭据时 code:-1 message:"未登录"）。"""
    import server.core.oauth_usage as ou
    calls = []
    monkeypatch.setattr(ou.httpx, "AsyncClient", make_httpx(calls, [(200, {
        "code": -1, "message": "未登录", "data": None,
    })]))
    r = asyncio.run(_lobsterai_usage("tok"))
    assert r["quotas"] == {}
    assert "未登录" in r["message"]


# ── quirks 档案 ─────────────────────────────────────
def test_lobsterai_quirks_registered():
    """域名方言：force_stream（上游只收 SSE）+ 静态头（UA/能力）。"""
    from server.core.provider_quirks import quirks_for
    q = quirks_for("https://lobsterai-server.youdao.com/api/proxy/v1")
    assert q is not None
    assert q.name == "lobsterai"
    assert q.force_stream is True
    assert q.default_headers["User-Agent"] == "LobsterAI/0.1.0"
    assert "kimi-k3-agentic-v1" in q.default_headers["X-LobsterAI-Client-Capabilities"]
