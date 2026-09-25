"""CodeArts（华为 DevEco）协议 + 适配器测试。

协议来源：Jet-Hub 源码逐行核对（github.com/zhengwuji/Jet-Hub，MIT）
+ 其 tests/e2e 里的真实实测记录。每条用例锁定的都是「反直觉、改错就出问题」
的硬约束（见每个 docstring）。

其中签名用例用的是**金标准向量**：用 node 直接执行 Jet-Hub 的 `src/sign.ts`
（固定时钟 2026-03-04T05:06:07.891Z）产出的 Authorization 串，逐字节比对。
改签名实现让这些用例失败时，别改期望值 —— 先确认是不是真的把协议改错了。
"""
import asyncio
import base64
import datetime
import hashlib
import json
import time

import pytest
from types import SimpleNamespace

import server.core.codearts as ca
from server.adapters import codearts_adapter as ad_mod
from server.adapters.codearts_adapter import CodeArtsAdapter


# ── 工具 ────────────────────────────────────────────
def _run(coro):
    return asyncio.run(coro)


# 金标准时钟（与生成向量的脚本一致）
GOLDEN_TS = datetime.datetime(2026, 3, 4, 5, 6, 7, 891000,
                              tzinfo=datetime.timezone.utc).timestamp()
GOLDEN_URL = "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions"
GOLDEN_BODY = (b'{"model":"deepseek-v4-flash","messages":'
               b'[{"role":"user","content":"hi"}],"stream":true}')
GOLDEN_POST_AUTH = (
    "SDK-HMAC-SHA256 Access=AKIDEXAMPLE,"
    "SignedHeaders=content-type;host;maas_type;x-sdk-content-sha256;"
    "x-sdk-date;x-security-token,"
    "Signature=ed5a401eebeb3e2c91aa6b8b84e2ca0a6c344eaff648a2dda644e04446da4609")
GOLDEN_GET_AUTH = (
    "SDK-HMAC-SHA256 Access=AKIDEXAMPLE,"
    "SignedHeaders=host;x-sdk-content-sha256;x-sdk-date;x-security-token,"
    "Signature=3b22c2ce5b9cbf2f99974d7a29051cde9cf4750ba4f3d366c38bb4bfd402701f")
GOLDEN_QUEUE_AUTH = (
    "SDK-HMAC-SHA256 Access=AKIDEXAMPLE,"
    "SignedHeaders=host;x-sdk-content-sha256;x-sdk-date;x-security-token,"
    "Signature=01a0a279249b5360acc235267c9ffe2ec959264ed7be4ffb285b235a30a8d40b")


# ══ 1. 签名（SDK-HMAC-SHA256）════════════════════════

def test_signature_matches_jet_hub_golden_post_with_maas_type():
    """金标准：POST + maas_type: benefit 的签名串（含 SignedHeaders 顺序）。

    这一条同时锁死：
    - canonical request 的第 5 段是**空行**（漏了段数就错，签名必不一致）；
    - `uri` 不以 / 结尾时补一个；
    - `maas_type` 进 SignedHeaders（glm-5.3-flash 的硬要求）。
    """
    headers = ca.signed_headers_for_request(
        "AKIDEXAMPLE", "SKEXAMPLE", "STEXAMPLE", "POST", GOLDEN_URL, GOLDEN_BODY,
        extra_signed_headers={"maas_type": "benefit"}, now=GOLDEN_TS)
    assert headers["Authorization"] == GOLDEN_POST_AUTH
    assert headers["x-sdk-content-sha256"] == hashlib.sha256(GOLDEN_BODY).hexdigest()
    assert headers["x-sdk-date"] == "20260304T050607Z"
    # host 必须剔除：由运行时按实际连接目标生成
    assert "host" not in headers


def test_signature_matches_golden_get_no_content_type():
    """金标准：GET 无 body → **不加 content-type**（加了反而验签不一致）。"""
    headers = ca.signed_headers_for_request(
        "AKIDEXAMPLE", "SKEXAMPLE", "STEXAMPLE", "GET",
        "https://snap-access.cn-north-4.myhuaweicloud.com/v1/model/builtin", b"",
        now=GOLDEN_TS)
    assert headers["Authorization"] == GOLDEN_GET_AUTH
    assert "content-type" not in headers
    assert headers["x-sdk-content-sha256"] == hashlib.sha256(b"").hexdigest()


def test_signature_matches_golden_get_with_query():
    """金标准：带 query 的 GET（排队状态轮询）—— query 原样进 canonical，不重排序。"""
    headers = ca.signed_headers_for_request(
        "AKIDEXAMPLE", "SKEXAMPLE", "STEXAMPLE", "GET",
        "https://snap-access.cn-north-4.myhuaweicloud.com/api/v1/queue/status"
        "?model=deepseek-v4-flash&task_id=abc123", b"", now=GOLDEN_TS)
    assert headers["Authorization"] == GOLDEN_QUEUE_AUTH


def test_canonical_request_has_empty_fifth_segment():
    """canonical request 七段，第 5 段是空串 —— 这是最容易写错的一处。"""
    canonical = ca.build_canonical_request(
        "GET", "/v1/model/builtin/", "", {"host": "h"}, "deadbeef")
    parts = canonical.split("\n")
    assert parts == ["GET", "/v1/model/builtin/", "", "host:h", "", "host", "deadbeef"]


def test_signed_headers_extra_signed_vs_unsigned():
    """两类额外头必须分清：
    - `maas_type`（signed）**必须**出现在 SignedHeaders 且随请求发送；
    - `Agent-Type`（unsigned）**绝不能**进 SignedHeaders（实测进即 401 APIG.0301）。
    """
    signed = ca.signed_headers_for_request(
        "AK", "SK", "ST", "GET", "https://x.cn/v1/model/builtin", b"",
        extra_signed_headers={"maas_type": "benefit"},
        extra_unsigned_headers=dict(ca.SNAP_UNSIGNED_HEADERS))
    assert "maas_type" in signed["Authorization"]
    assert "agent-type" not in signed["Authorization"].lower()
    assert "x-language" not in signed["Authorization"].lower()
    # 未签名头仍要发出去（服务端路由需要）
    assert signed["Agent-Type"] == "PromptCenter"
    assert signed["X-Language"] == "zh-cn"


def test_maas_type_only_for_benefit_models():
    """`glm-5.3-flash` 需要 maas_type: benefit（否则 002002009.404）；其余模型不加。"""
    assert ca.maas_type_headers("glm-5.3-flash") == {"maas_type": "benefit"}
    assert ca.maas_type_headers("GLM-5.2") is None
    assert ca.maas_type_headers("deepseek-v4-flash") is None
    assert ca.is_benefit_model("glm-5.3-flash") is True


def test_signed_headers_for_request_is_pure_no_side_effects():
    """同一输入两次签名结果一致（除时间戳外无随机量）——签名必须是确定性的。"""
    a = ca.sign_request_huawei("AK", "SK", "ST", "POST", GOLDEN_URL, GOLDEN_BODY,
                               now=GOLDEN_TS)
    b = ca.sign_request_huawei("AK", "SK", "ST", "POST", GOLDEN_URL, GOLDEN_BODY,
                               now=GOLDEN_TS)
    assert a == b


def test_deepseek_v4_detection_is_exact():
    """DSML 判定是**精确** id 匹配：`deepseek-v4-flash-0731` 不该被判为 v4。"""
    assert ca.is_deepseek_v4_model("deepseek-v4-flash") is True
    assert ca.is_deepseek_v4_model("deepseek-v4-pro") is True
    assert ca.is_deepseek_v4_model("deepseek-v4-flash-0731") is False
    assert ca.is_deepseek_v4_model("GLM-5.2") is False


# ══ 2. 授权 URL / 回调 ═══════════════════════════════

def test_oauth_login_url_code_challenge_method_is_sha256():
    """⚠️ `code_challenge_method=SHA-256`（**不是** S256）。

    写错时 portal 会静默回退旧 ticket 流程：用户照样登录成功，但拿到的凭据
    **没有 refresh_token**，此后永远无法续期。
    """
    pkce = ca.PkcePair("verifier-1", "challenge-1")
    url = ca.build_oauth_login_url(18090, pkce, "ticket-1")
    assert "code_challenge_method=SHA-256" in url
    assert "code_challenge_method=S256" not in url
    assert url.startswith("https://codearts.huaweicloud.com/portal/authorize?")
    assert "client_id=codearts-agent" in url
    assert "uri_scheme=codearts-agent" in url
    assert "plugin-name=snap_AIIDE" in url
    assert "plugin-version=5.2.0" in url
    assert "theme=2" in url and "locale=zh-cn" in url
    assert "port=18090" in url
    assert "code_challenge=challenge-1" in url
    assert "ticket_id=ticket-1" in url


def test_oauth_login_url_has_no_auth_callback_url():
    """⚠️ **不要**加 `auth_callback_url` —— portal 仅凭 port 构造回调，
    多余参数会被视为异常并回退旧流程（登录页会莫名其妙走 ticket 分支）。"""
    url = ca.build_oauth_login_url(18090, ca.PkcePair("v", "c"), "t")
    assert "auth_callback_url" not in url


def test_callback_port_must_be_at_least_10000():
    """⚠️ 端口必须 ≥10000 —— 低端口会被 portal 拒绝。

    这里选择**抛异常**而不是静默换端口：端口同时出现在授权 URL 与换票
    redirect_uri 里，静默替换会让两者不一致，换票失败还会指向看不出根因的地方。
    """
    with pytest.raises(ValueError) as e:
        ca.ensure_callback_port(8000)
    assert "10000" in str(e.value)
    with pytest.raises(ValueError):
        ca.ensure_callback_port(9999)
    assert ca.ensure_callback_port(10000) == 10000
    with pytest.raises(ValueError):
        ca.ensure_callback_port(70000)
    with pytest.raises(ValueError):
        ca.ensure_callback_port("abc")


def test_build_oauth_login_url_rejects_low_port():
    with pytest.raises(ValueError):
        ca.build_oauth_login_url(3000, ca.PkcePair("v", "c"), "t")


def test_pick_callback_port_always_high():
    """随机端口恒 ≥10000（低端口会被 portal 拒绝）。"""
    for _ in range(200):
        assert ca.pick_callback_port() >= ca.CODEARTS_MIN_CALLBACK_PORT


def test_redirect_uri_is_127_0_0_1_with_path():
    """redirect_uri 主机名锁死 127.0.0.1（不是 localhost）+ 路径 /oauth/callback。"""
    assert ca.redirect_uri_for_port(18090) == "http://127.0.0.1:18090/oauth/callback"


def test_pkce_challenge_is_s256_of_verifier():
    """challenge 是标准 S256(verifier)；URL 上的 `SHA-256` 只是字面量。

    两者容易混：portal 按 S256 校验 challenge，按字面量识别流程。
    """
    pkce = ca.generate_pkce_pair()
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(pkce.code_verifier.encode()).digest()).rstrip(b"=").decode()
    assert pkce.code_challenge == expect
    # verifier 是 48 字节 base64url（64 字符）
    assert len(pkce.code_verifier) == 64


def test_parse_callback_payload_supports_url_and_plain():
    """回调解析同时支持「整段 URL」与分离参数；旧流程的 secret 也要认。"""
    r = ca.parse_callback_payload(
        callback_url="http://127.0.0.1:18090/oauth/callback?code=c1&state=s1")
    assert r["kind"] == "oauth" and r["code"] == "c1" and r["state"] == "s1"
    r2 = ca.parse_callback_payload(callback_url="http://127.0.0.1:18090/oauth/callback?secret=sec1")
    assert r2["kind"] == "ticket" and r2["secret"] == "sec1"
    r3 = ca.parse_callback_payload(code="c9", state="s9")
    assert r3["kind"] == "oauth" and r3["code"] == "c9"
    assert ca.parse_callback_payload()["kind"] == "none"
    # 半截 URL 不能抛异常
    assert ca.parse_callback_payload(callback_url="not a url")["kind"] == "none"


def test_portal_login_result_url_shape():
    url = ca.build_portal_login_result_url(True)
    assert url.startswith("https://codearts.huaweicloud.com/portal/login?")
    assert "login_succeed=true" in url and "uri_scheme=codearts-agent" in url
    assert "login_succeed=false" in ca.build_portal_login_result_url(False)


# ══ 3. DPoP（token 请求的硬要求）══════════════════════

def test_dpop_jws_is_es256_with_embedded_jwk():
    """DPoP 头是 ES256 JWS，且头里内嵌**公钥** jwk；payload 无 ath。"""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives import hashes

    kp = ca.generate_dpop_key_pair()
    jws = ca.sign_dpop_jws(kp["private_key_jwk"], "POST",
                           ca.CODEARTS_STS_TOKEN_ENDPOINT, now=1700000000.0)
    header_b64, payload_b64, sig_b64 = jws.split(".")

    def _dec(part):
        pad = "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part + pad))

    header = _dec(header_b64)
    assert header["alg"] == "ES256"
    assert header["typ"] == "dpop+jwt"
    assert header["jwk"]["kty"] == "EC" and header["jwk"]["crv"] == "P-256"
    assert "d" not in header["jwk"]          # 公钥不能泄漏私钥
    assert set(header["jwk"]) == {"kty", "crv", "x", "y"}

    payload = _dec(payload_b64)
    assert payload["htm"] == "POST"
    assert payload["htu"] == ca.CODEARTS_STS_TOKEN_ENDPOINT   # htu 是完整 URL
    assert payload["iat"] == 1700000000
    assert len(payload["jti"]) == 64                          # 32 字节 hex
    assert "ath" not in payload                               # CodeArts 不绑定 access token

    # 签名可被公钥验证（raw r||s 64 字节，非 DER）
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicNumbers
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    raw = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    assert len(raw) == 64
    r, s = decode_dss_signature(encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")))
    pk = EllipticCurvePublicNumbers(
        x=int.from_bytes(base64.urlsafe_b64decode(
            header["jwk"]["x"] + "=" * (-len(header["jwk"]["x"]) % 4)), "big"),
        y=int.from_bytes(base64.urlsafe_b64decode(
            header["jwk"]["y"] + "=" * (-len(header["jwk"]["y"]) % 4)), "big"),
        curve=ec.SECP256R1()).public_key()
    pk.verify(encode_dss_signature(r, s),
              f"{header_b64}.{payload_b64}".encode(), ec.ECDSA(hashes.SHA256()))


def test_dpop_jws_requires_private_key():
    with pytest.raises(Exception):
        ca.sign_dpop_jws({"kty": "EC", "crv": "P-256", "x": "a", "y": "b"}, "POST", "u")


# ══ 4. token 换票 / 续期 ═════════════════════════════

def _token_resp(ak="AK1", sk="SK1", st="ST1", exp="2026-08-25T12:34:56Z",
                rt="RT-new"):
    return {"credentials": {"access_key_id": ak, "secret_access_key": sk,
                            "security_token": st, "expiration": exp},
            "refresh_token": rt}


def _patch_httpx(monkeypatch, module, results):
    """results: [(status, payload)]。记录每次请求的 url/headers/content。"""
    results = list(results)
    calls = []

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            try:
                self.text = json.dumps(payload)
            except (TypeError, ValueError):
                self.text = str(payload)

        def json(self):
            if isinstance(self._payload, dict) or isinstance(self._payload, list):
                return self._payload
            raise ValueError("not json")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _next(self):
            return _Resp(*results.pop(0)) if results else _Resp(200, {})

        async def post(self, url, headers=None, content=None, json=None):
            calls.append({"m": "POST", "url": url, "headers": headers or {},
                          "content": content, "json": json})
            return self._next()

        async def get(self, url, headers=None, params=None):
            calls.append({"m": "GET", "url": url, "headers": headers or {},
                          "params": params})
            return self._next()

        def stream(self, method, url, headers=None, content=None):
            calls.append({"m": "STREAM", "url": url, "headers": headers or {},
                          "content": content})
            return _StreamResp(results.pop(0) if results else (200, []), calls)

    monkeypatch.setattr(module.httpx, "AsyncClient", _Client)
    return calls


class _StreamResp:
    """伪造 httpx 的 stream() 上下文（够用即可：状态码 + 逐行 SSE）。"""

    def __init__(self, spec, calls):
        status, lines = spec if isinstance(spec, tuple) else (200, spec)
        self.status_code = status
        self._lines = lines
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return json.dumps({"error_code": "APIG.0301",
                           "error_msg": "verify ak sk signature fail"}).encode()

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def test_request_token_sends_dpop_and_form_body(monkeypatch):
    """token 请求必须带 DPoP 头，body 是 **form-urlencoded**（不是 JSON）。"""
    calls = _patch_httpx(monkeypatch, ca, [(200, _token_resp())])
    kp = ca.generate_dpop_key_pair()
    data, err, dead = _run(ca.request_token(
        {"client_id": "codearts-agent", "grant_type": "authorization_code"},
        kp["private_key_jwk"]))
    assert err == "" and dead is False
    assert data["credentials"]["access_key_id"] == "AK1"
    c = calls[-1]
    assert c["url"] == ca.CODEARTS_STS_TOKEN_ENDPOINT
    assert "DPoP" in c["headers"]
    assert c["headers"]["DPoP"].count(".") == 2   # header.payload.signature
    assert "application/x-www-form-urlencoded" in c["headers"]["Content-Type"]
    assert c["json"] is None                       # 不能是 JSON body
    assert "grant_type=authorization_code" in c["content"]


def test_request_token_requires_dpop_key():
    data, err, dead = _run(ca.request_token({"a": "b"}, {}))
    assert data is None and "DPoP" in err


def test_exchange_authorization_code_body_fields(monkeypatch):
    """换票 body 五字段照抄源码：client_id/code/code_verifier/grant_type/redirect_uri。"""
    calls = _patch_httpx(monkeypatch, ca, [(200, _token_resp())])
    kp = ca.generate_dpop_key_pair()
    cred, err = _run(ca.exchange_authorization_code(
        "code-1", "verifier-1", 18090, kp["private_key_jwk"]))
    assert err == ""
    assert cred.access_key_id == "AK1" and cred.secret_access_key == "SK1"
    assert cred.security_token == "ST1"
    assert cred.refresh_token == "RT-new"
    assert cred.code_verifier == "verifier-1"
    assert cred.dpop_private_key_jwk is not None
    body = calls[-1]["content"]
    assert "client_id=codearts-agent" in body
    assert "code=code-1" in body
    assert "code_verifier=verifier-1" in body
    assert "grant_type=authorization_code" in body
    # redirect_uri 必须带同一个 port
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A18090%2Foauth%2Fcallback" in body


def test_exchange_failure_when_credentials_missing(monkeypatch):
    """响应没有 credentials → 换票失败（绝不能把 error 响应当凭据存）。"""
    _patch_httpx(monkeypatch, ca, [(200, {"error": "invalid_grant",
                                          "error_code": "InvalidGrant"})])
    kp = ca.generate_dpop_key_pair()
    cred, err = _run(ca.exchange_authorization_code("c", "v", 18090, kp["private_key_jwk"]))
    assert cred is None and "失败" in err


def test_refresh_token_rotation_returns_new_refresh_token(monkeypatch):
    """⚠️ refresh_token **一次性轮换**：返回值里的新 refresh_token 必须交给
    调用方立即回写；丢弃它等于把凭据废掉（下次续期必然 STS5.1806）。"""
    calls = _patch_httpx(monkeypatch, ca, [(200, _token_resp(rt="RT-2"))])
    kp = ca.generate_dpop_key_pair()
    cred, err, dead = _run(ca.refresh_token("RT-1", "verifier-1",
                                            kp["private_key_jwk"]))
    assert err == "" and dead is False
    assert cred.refresh_token == "RT-2"          # 新值，不是 RT-1
    assert cred.refresh_token != "RT-1"
    body = calls[-1]["content"]
    assert "grant_type=refresh_token" in body
    assert "refresh_token=RT-1" in body
    assert "code_verifier=verifier-1" in body


def test_refresh_token_reused_error_message(monkeypatch):
    """⚠️ 并发刷新互相作废的直接症状：`STS5.1806 the refresh token has been used`。

    必须给出可执行的说明（提示并发续期 + 重新登录），而不是把原始文案丢给用户。
    """
    _patch_httpx(monkeypatch, ca, [(400, {
        "error": "invalid_request",
        "error_code": "STS5.1806",
        "error_msg": "the refresh token has been used",
    })])
    kp = ca.generate_dpop_key_pair()
    cred, err, dead = _run(ca.refresh_token("RT-used", "v", kp["private_key_jwk"]))
    assert cred is None
    assert dead is True
    assert "STS5.1806" in err or "已被使用" in err   # 原始码或说明至少有一个
    assert "并发" in err or "重新登录" in err         # 必须给可执行指引


def test_refresh_token_dead_detection():
    """终态失效判据（对齐 src/oauth.ts:127-135 的三种 error_code）。

    `InvalidDPoPHeader` 也算终态 —— 不然会每 10 分钟无限重试。
    """
    assert ca.is_refresh_token_dead_error("invalid_grant") is True
    assert ca.is_refresh_token_dead_error("ExpiredRefreshToken") is True
    assert ca.is_refresh_token_dead_error("InvalidDPoPHeader") is True
    assert ca.is_refresh_token_dead_error(
        "STS5.1806 the refresh token has been used") is True
    assert ca.is_refresh_token_dead_error("network timeout") is False
    assert ca.is_refresh_token_reused_error("STS5.1806") is True
    assert ca.is_refresh_token_reused_error("the refresh token has been used") is True
    assert ca.is_refresh_token_reused_error("ExpiredRefreshToken") is False


def test_refresh_without_refresh_token_is_terminal():
    """没有 refresh_token（旧 ticket 凭据）→ 终态：不可静默续期，应提示重登。"""
    cred, err, dead = _run(ca.refresh_token("", "v", ca.generate_dpop_key_pair()["private_key_jwk"]))
    assert cred is None and dead is True and "refresh_token" in err


def test_expires_in_from_iso_and_fallback():
    """expires_at 缺失/不可解析时回退 24h（**不返回 0**）。

    返回 0 会让刷新调度每轮都触发，而 refresh_token 一次性 ——
    一次多余刷新就废掉凭据。
    """
    now = time.time()
    future = datetime.datetime.fromtimestamp(now + 3600, tz=datetime.timezone.utc)
    v = ca.expires_in_from(future.isoformat())
    assert 3500 < v <= 3600
    assert ca.expires_in_from("") == 86400
    assert ca.expires_in_from("garbage") == 86400
    assert ca.expires_in_from("2020-01-01T00:00:00Z") == 60   # 已过期 → 下限 60


def test_is_credential_expired_missing_field_means_not_expired():
    """`expires_at` 缺失 **不**判过期：旧 ticket 凭据没有该字段，
    判过期会引发一轮注定失败的刷新。"""
    assert ca.is_credential_expired({}) is False
    assert ca.is_credential_expired({"expires_at": "garbage"}) is False
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    assert ca.is_credential_expired({"expires_at": past.isoformat()}) is True
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    assert ca.is_credential_expired({"expires_at": future.isoformat()}) is False


def test_parse_ticket_response_both_shapes():
    """旧 ticket 流程的两种响应形态：`credential` 内嵌 / `result` 平铺。

    字段别名一个都不能漏（securitytoken vs securityToken、
    access vs accessKeyId）—— 实测两种都出现过。
    """
    a = ca.parse_ticket_response({
        "credential": {"access": "AK", "secret": "SK", "securitytoken": "ST",
                       "expires_at": "2026-08-25T00:00:00Z"},
        "domain_id": "d1", "user_id": "u1", "user_name": "n1"})
    assert (a.access_key_id, a.secret_access_key, a.security_token) == ("AK", "SK", "ST")
    assert a.user_id == "u1" and a.user_name == "n1"
    # camelCase securityToken
    b = ca.parse_ticket_response({
        "credential": {"access": "AK2", "secret": "SK2", "securityToken": "ST2"}})
    assert b.security_token == "ST2"
    # result 平铺形态
    c = ca.parse_ticket_response({
        "result": {"accessKeyId": "AK3", "secretAccessKey": "SK3",
                   "securityToken": "ST3", "expiration": "2026-08-25T00:00:00Z"}})
    assert (c.access_key_id, c.secret_access_key, c.security_token) == ("AK3", "SK3", "ST3")
    # 未完成（无 AK/ST）→ None，而不是一个半残凭据
    assert ca.parse_ticket_response({"credential": {"access": "", "securitytoken": ""}}) is None
    assert ca.parse_ticket_response(None) is None


def test_ticket_flow_credential_has_no_refresh_token(monkeypatch):
    """⚠️ 旧 ticket 流程的凭据**没有 refresh_token**（据此可判断不可续期）。"""
    _patch_httpx(monkeypatch, ca, [(200, {
        "credential": {"access": "AK", "secret": "SK", "securitytoken": "ST"}})])
    cred, err = _run(ca.exchange_ticket("ticket-1", "secret-1", max_attempts=1))
    assert err == "" and cred is not None
    assert cred.refresh_token == ""            # 如实为空，不伪造


# ══ 5. 消息序列化（CodeArts 方言）══════════════════════

def test_assistant_messages_always_carry_reasoning_content():
    """⚠️ assistant 历史**必须**带 `reasoning_content` 字段（无推理时空串）。

    字段缺失时后端直接 400「Missing `reasoning_content` field」——
    deepseek-v4-flash/pro 的硬校验，整条会话作废。
    """
    wire = ca.serialize_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
        {"role": "assistant", "content": "ok2",
         "reasoning_content": "thinking..."},
    ])
    assert wire[0] == {"role": "user", "content": "hi"}
    assert wire[1]["role"] == "assistant"
    assert "reasoning_content" in wire[1] and wire[1]["reasoning_content"] == ""
    assert wire[2]["reasoning_content"] == "thinking..."


def test_reasoning_block_array_folded_into_reasoning_content():
    """harness 把推理放在 [{type:'reasoning'}] 块里 → 折叠成 reasoning_content。"""
    wire = ca.serialize_messages([
        {"role": "assistant", "content": [
            {"type": "reasoning", "text": "think-A"},
            {"type": "reasoning", "text": "think-B"},
            {"type": "text", "text": "answer"},
        ]},
    ])
    assert wire[0]["content"] == "answer"
    assert wire[0]["reasoning_content"] == "think-Athink-B"


def test_plain_string_user_message_is_not_emptied():
    """纯文本 user 消息必须原样透传。

    用「块数组」的取值路径会把字符串 content 变成空串，模型看不到任务指令
    （Jet-Hub 注释里明确点出的坑）。
    """
    wire = ca.serialize_messages([{"role": "user", "content": "请写一个函数"}])
    assert wire == [{"role": "user", "content": "请写一个函数"}]


def test_tool_results_expanded_to_separate_tool_messages():
    """搭载在 user 消息里的 tool-result 块 → 展开成独立的 role:'tool' 消息。

    只有 tool-result、没有文本的 user 消息**不保留**空的 user 条目
    （对齐 `src/llm-adapter.ts:142` 的 `text || toolResults.length === 0` 判据：
    塞一条 content 为空的 user 消息会让后端把工具结果当成一轮空对话）。
    """
    wire = ca.serialize_messages([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read", "arguments": '{"p":"x"}'}}]},
        {"role": "user", "content": [
            {"type": "tool-result", "toolCallId": "c1", "content": "file body"}]},
    ])
    assert wire[0]["tool_calls"][0]["id"] == "c1"
    assert wire[1] == {"role": "tool", "tool_call_id": "c1", "content": "file body"}
    assert len(wire) == 2
    # 带文本的 user 消息要保留（文本 + 展开的工具结果并存）
    wire2 = ca.serialize_messages([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "user", "content": [
            {"type": "text", "text": "继续"},
            {"type": "tool-result", "toolCallId": "c1", "content": "body"}]},
    ])
    assert wire2[1] == {"role": "user", "content": "继续"}
    assert wire2[2] == {"role": "tool", "tool_call_id": "c1", "content": "body"}


def test_orphan_tool_call_is_dropped():
    """孤儿 tool_call（没有对应结果）必须剔除。

    否则这条坏历史会被每次请求重放，后端对之后每条消息都 400 ——
    用户看到的是「任务突然中断，此后发什么都没回复」。
    """
    wire = ca.serialize_messages([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "orphan", "type": "function",
             "function": {"name": "write", "arguments": "{}"}}]},
        {"role": "user", "content": "continue"},
    ])
    assert "tool_calls" not in wire[0]
    assert wire[1]["role"] == "user"


def test_partial_tool_pairing_dropped_entirely():
    """一批 tool_calls 只有**全部**有结果才保留：部分保留照样留下无结果的调用。"""
    wire = ca.serialize_messages([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "user", "content": [
            {"type": "tool-result", "toolCallId": "c1", "content": "r1"}]},
    ])
    assert "tool_calls" not in wire[0]
    assert all(m["role"] != "tool" for m in wire)


def test_tool_arguments_normalized_to_object():
    """arguments 空串/非法/非对象 → `{}`（空串会让 schema 校验报
    "arguments must be an object" 并卡死会话）。"""
    assert ca.normalize_tool_arguments("") == "{}"
    assert ca.normalize_tool_arguments("not json") == "{}"
    assert ca.normalize_tool_arguments("[1,2]") == "{}"
    assert ca.normalize_tool_arguments("null") == "{}"
    assert ca.normalize_tool_arguments('{"a":1}') == '{"a":1}'


def test_content_to_text_drops_non_text_blocks():
    """非 text 块（图片等）必须丢弃：端点会因它返回**空流**。"""
    assert ca.content_to_text([{"type": "image_url", "image_url": {"url": "x"}},
                               {"type": "text", "text": "看"}]) == "看"
    assert ca.content_to_text("plain") == "plain"
    assert ca.content_to_text(None) == ""


def test_system_prompt_injected_at_top():
    """顶层 system 提示必须插进 messages（后端只认 messages 里的 system）。

    缺失时模型看不到系统指令 —— 实测表现为会话标题变成 prompt 回显。
    """
    wire = ca.serialize_messages([{"role": "user", "content": "hi"}],
                                 system="你是标题生成器")
    assert wire[0] == {"role": "system", "content": "你是标题生成器"}
    assert wire[1]["role"] == "user"


def test_dsml_prompt_sits_after_system_before_user():
    """DSML 工具说明插在 system **之后、user 之前**。

    推到 messages 末尾会让 deepseek-v4 把思考写进 content（正文）而不是
    reasoning_content，思考泄露给用户。
    """
    wire = ca.serialize_messages([{"role": "user", "content": "hi"}],
                                 system="SYS", dsml_system_prompt="DSML-TOOLS")
    roles = [m["role"] for m in wire]
    assert roles == ["system", "system", "user"]
    assert wire[0]["content"] == "SYS"
    assert wire[1]["content"] == "DSML-TOOLS"


# ══ 6. 请求体 ════════════════════════════════════════

def _req(model="deepseek-v4-flash", messages=None, tools=None, **kw):
    """最小 ChatCompletionRequest 替身（含可调 model_dump）。"""
    def _dump(**_kwargs):
        return dict(self_dict)

    class _Msg:
        def __init__(self, d):
            self._d = d
            self.role = d.get("role")
            self.content = d.get("content")

        def model_dump(self, **_kwargs):
            return dict(self._d)

    req = SimpleNamespace(
        model=model,
        messages=[_Msg(m) for m in (messages or [{"role": "user", "content": "hi"}])],
        tools=tools, max_tokens=kw.get("max_tokens"),
        temperature=None, top_p=None, stop=None, seed=None,
        presence_penalty=None, frequency_penalty=None)
    self_dict = {}
    return req


def test_dsml_mode_does_not_send_tools_field():
    """⚠️ DSML 模式下请求体**不含 `tools` 字段**。

    发了 tools，模型就走标准 tool_calls 一次性打包路径：生成大参数期间 SSE
    静默数十秒，APIG 网关 ~60s 空闲必然掐断（实测 300 行 write 即断）。
    改为把 schema 注入 system + DSML 语法流式输出，全程有数据流。
    """
    tools = [{"type": "function",
              "function": {"name": "write", "description": "写文件",
                           "parameters": {"type": "object"}}}]
    body = ca.build_chat_body(_req(tools=tools), session_id="s1", dsml=True,
                              tools=tools)
    assert "tools" not in body
    # schema 以文本注入 system
    sys_msgs = [m for m in body["messages"] if m["role"] == "system"]
    assert sys_msgs and "DSML" in sys_msgs[0]["content"]
    assert "write" in sys_msgs[0]["content"]


def test_non_dsml_mode_sends_tools_field():
    """非 deepseek-v4 模型保持标准 tool_calls（不经 APIG 网关，无 60s 断连）。"""
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
    body = ca.build_chat_body(_req(model="GLM-5.2", tools=tools),
                              session_id="s1", dsml=False, tools=tools)
    assert body["tools"] == tools
    assert all(m["role"] != "system" for m in body["messages"])


def test_dsml_decision_helper():
    """DSML 只对 deepseek-v4 生效（且必须有工具才有意义）。"""
    assert ca.needs_dsml_tool_mode("deepseek-v4-pro") is True
    assert ca.needs_dsml_tool_mode("GLM-5.2") is False
    assert ca.needs_dsml_tool_mode("openpangu-2.0-flash") is False


def test_body_carries_required_ide_fields():
    """请求体的固定字段（逐条对齐 llm-adapter.ts:895-920）。

    - `stream` 恒 true（上游只有 SSE）；
    - `prompt_cache_key` 缺失 → 缓存命中恒为 0；
    - `max_tokens` 默认 65536（131072 会被后端拒成空流）；
    - `tool_stream` / `include` / `reasoning_summary` 对齐 IDE 请求体。
    """
    body = ca.build_chat_body(_req(), session_id="sess-1")
    assert body["stream"] is True
    assert body["prompt_cache_key"] == "sess-1"
    assert body["max_tokens"] == 65536
    assert body["tool_stream"] is True
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning_summary"] == "auto"


def test_max_tokens_override_wins_but_zero_falls_back():
    assert ca.resolve_max_tokens(500) == 500
    assert ca.resolve_max_tokens(None) == 65536
    assert ca.resolve_max_tokens(0) == 65536
    assert ca.resolve_max_tokens("bad") == 65536


# ══ 7. DSML 流式解析（三态状态机）══════════════════════

def _dsml_block(name="write", param="file_path", value="/tmp/a.txt", string=True):
    attr = ' string="true"' if string else ''
    return (ca.DSML_TOOL_CALLS_OPEN
            + f'<｜DSML｜invoke name="{name}">'
            + f'<｜DSML｜parameter name="{param}"{attr}>{value}</｜DSML｜parameter>'
            + ca.DSML_INVOKE_CLOSE
            + ca.DSML_TOOL_CALLS_CLOSE)


def test_dsml_extractor_single_chunk():
    ex = ca.DsmlContentExtractor()
    out = ex.feed("前文" + _dsml_block() + "后文")
    assert out["text"] == "前文后文"
    assert len(out["tool_calls"]) == 1
    assert out["tool_calls"][0]["name"] == "write"
    assert json.loads(out["tool_calls"][0]["arguments"]) == {"file_path": "/tmp/a.txt"}


def test_dsml_extractor_split_across_chunks():
    """DSML 块跨多个 SSE chunk 分片到达时必须仍能解析（增量友好）。"""
    ex = ca.DsmlContentExtractor()
    block = "你好" + _dsml_block(name="read", param="path", value="a.py")
    texts, calls = "", []
    for i in range(0, len(block), 5):
        out = ex.feed(block[i:i + 5])
        texts += out["text"]
        calls.extend(out["tool_calls"])
    rest = ex.flush()
    texts += rest["text"]
    assert texts == "你好"
    assert len(calls) == 1 and calls[0]["name"] == "read"


def test_dsml_extractor_thought_goes_to_reasoning():
    """`<thought>` 内容分流到 reasoning（显示在思考区，不泄漏到正文）。"""
    ex = ca.DsmlContentExtractor()
    out = ex.feed("<thought>内部推理</thought>正文")
    assert out["reasoning"] == "内部推理"
    assert out["text"] == "正文"


def test_dsml_extractor_thought_split_across_chunks():
    ex = ca.DsmlContentExtractor()
    o1 = ex.feed("A<thou")
    assert o1["text"] == "A" and o1["reasoning"] == ""
    o2 = ex.feed("ght>深思</thought>")
    assert o2["reasoning"] == "深思"


def test_dsml_extractor_flush_truncated_dsml_keeps_open_tag():
    """流结束时残留的不完整 DSML 块必须**补回开标签**按正文放行。

    直接丢开标签会让 UI 出现缺头残片（用户看到莫名的 </｜DSML｜invoke>）；
    整体丢弃则会吞掉用户可见内容。
    """
    ex = ca.DsmlContentExtractor()
    out = ex.feed("abc" + ca.DSML_TOOL_CALLS_OPEN + '<｜DSML｜invoke name="x"')
    assert out["text"] == "abc"
    rest = ex.flush()
    assert rest["text"].startswith(ca.DSML_TOOL_CALLS_OPEN)
    assert "invoke" in rest["text"]


def test_dsml_extractor_thought_streams_before_close():
    """in-thought 期间内容**立即**作为 reasoning 放行（不是等闭合）。

    若等闭合才吐，长思考期间 SSE 无任何输出 → 触发网关空闲断连。
    只有缓冲区末尾恰好是闭合标签的不完整前缀时才保留。
    """
    ex = ca.DsmlContentExtractor()
    out = ex.feed("<thought>未闭合的推理")
    assert out["reasoning"] == "未闭合的推理"
    assert out["text"] == ""
    # 末尾是闭合标签前缀 → 必须保留等下次 feed（否则会吐出半个标签）
    ex2 = ca.DsmlContentExtractor()
    o1 = ex2.feed("<thought>推理</thou")
    assert o1["reasoning"] == "推理"
    o2 = ex2.feed("ght>正文")
    assert o2["text"] == "正文"


def test_dsml_extractor_flush_in_thought_keeps_reasoning_channel():
    """截断的 thought 块在 flush 时按 reasoning 放行（不能泄漏到正文）。"""
    ex = ca.DsmlContentExtractor()
    ex.feed("<thought>推理前缀</thou")   # 尾部残留半个闭合标签
    rest = ex.flush()
    assert rest["reasoning"] == "</thou"
    assert rest["text"] == ""


def test_dsml_extractor_two_channels_must_be_separate():
    """⚠️ content 与 reasoning_content 必须各用一个提取器实例。

    共用时：一个通道进了 in-thought 状态后，会把另一个通道的 DSML 块
    当成 thought 内容吞掉，工具调用丢失、DSML 标签经回退泄漏到正文
    （Jet-Hub 记录的实测缺陷）。
    """
    shared = ca.DsmlContentExtractor()
    # content 通道先开一个 thought
    shared.feed("<thought>思考中")
    # reasoning 通道送来一个完整的 DSML 块 —— 共用实例会把它当 thought 内容
    swallowed = shared.feed(_dsml_block())
    assert swallowed["tool_calls"] == []          # 被吞掉了：这就是共用的恶果
    # 分开用则正常
    c_ex, r_ex = ca.DsmlContentExtractor(), ca.DsmlContentExtractor()
    c_ex.feed("<thought>思考中")
    ok = r_ex.feed(_dsml_block())
    assert len(ok["tool_calls"]) == 1


def test_dsml_param_scalar_coercion():
    """模型常把数字/布尔参数误标 string="true" —— 必须还原始类型。

    否则工具 schema 校验报 `"offset" must be a number`，调用失败。
    """
    block = (ca.DSML_TOOL_CALLS_OPEN + '<｜DSML｜invoke name="read">'
             + '<｜DSML｜parameter name="offset" string="true">1304</｜DSML｜parameter>'
             + '<｜DSML｜parameter name="limit" string="true">true</｜DSML｜parameter>'
             + '<｜DSML｜parameter name="path" string="true">a.py</｜DSML｜parameter>'
             + ca.DSML_INVOKE_CLOSE + ca.DSML_TOOL_CALLS_CLOSE)
    calls = ca.parse_dsml_tool_calls(block)
    args = json.loads(calls[0]["arguments"])
    assert args["offset"] == 1304 and isinstance(args["offset"], int)
    assert args["limit"] is True
    assert args["path"] == "a.py"


def test_dsml_param_json_quoted_number_without_string_attr():
    """deepseek-v4-pro 会把数字用 JSON 引号包裹且不加 string="true" → 也要还原。"""
    block = (ca.DSML_TOOL_CALLS_OPEN + '<｜DSML｜invoke name="read">'
             + '<｜DSML｜parameter name="offset">"840"</｜DSML｜parameter>'
             + ca.DSML_INVOKE_CLOSE + ca.DSML_TOOL_CALLS_CLOSE)
    args = json.loads(ca.parse_dsml_tool_calls(block)[0]["arguments"])
    assert args["offset"] == 840


def test_dsml_param_array_stays_array():
    block = (ca.DSML_TOOL_CALLS_OPEN + '<｜DSML｜invoke name="todo_write">'
             + '<｜DSML｜parameter name="todos" string="true">'
             + '[{"content":"a"}]</｜DSML｜parameter>'
             + ca.DSML_INVOKE_CLOSE + ca.DSML_TOOL_CALLS_CLOSE)
    args = json.loads(ca.parse_dsml_tool_calls(block)[0]["arguments"])
    assert args["todos"] == [{"content": "a"}]


def test_dsml_multiple_invokes_in_one_block():
    block = (ca.DSML_TOOL_CALLS_OPEN
             + '<｜DSML｜invoke name="a"><｜DSML｜parameter name="x" string="true">1</｜DSML｜parameter></｜DSML｜invoke>'
             + '<｜DSML｜invoke name="b"><｜DSML｜parameter name="y" string="true">2</｜DSML｜parameter></｜DSML｜invoke>'
             + ca.DSML_TOOL_CALLS_CLOSE)
    calls = ca.parse_dsml_tool_calls(block)
    assert [c["name"] for c in calls] == ["a", "b"]


# ══ 8. 排队 / 错误分类 ═══════════════════════════════

def test_queue_error_detection_requires_400_and_code():
    """排队判据：HTTP **400** 且命中业务码/文案（其它状态码不是排队）。"""
    assert ca.is_queue_error(400, '{"error_code":"TM.00001041"}') is True
    assert ca.is_queue_error(400, "peak hours, try again after 2h") is True
    assert ca.is_queue_error(400, "high demand") is True
    assert ca.is_queue_error(500, "TM.00001041") is False
    assert ca.is_queue_error(400, "model not found") is False


def test_sse_queue_error_code_detection():
    """HTTP 200 + SSE 内嵌 `InferHub.ModelArts.81111.429`（TPM 超限）。

    不识别它就把流当正常结束 —— 用户看到「思考后无输出」。
    """
    assert ca.is_sse_queue_error_code("TM.00001041") is True
    assert ca.is_sse_queue_error_code("InferHub.ModelArts.81111.429") is True
    assert ca.is_sse_queue_error_code("InferHub.002002009.404") is False


def test_parse_queue_status_only_four_states():
    """状态只认四个取值；`queue_position` 缺失给哨兵 -1（区分「未知」与 0）。"""
    assert ca.parse_queue_status({"status": "waiting", "queue_position": 3}) == {
        "status": "waiting", "queue_position": 3, "message": ""}
    assert ca.parse_queue_status({"status": "queue_full"})["queue_position"] == -1
    assert ca.parse_queue_status({"status": "weird"}) is None
    assert ca.parse_queue_status(None) is None


def test_queue_budget_is_30_minutes():
    """排队上限 180 × 10s = 30 分钟（对齐 deveco-code 参考实现）。"""
    assert ca.QUEUE_MAX_ATTEMPTS == 180
    assert ca.QUEUE_RETRY_DELAY_SECONDS == 10.0
    assert ca.QUEUE_MAX_ATTEMPTS * ca.QUEUE_RETRY_DELAY_SECONDS == 1800


def test_auth_error_detection():
    """鉴权失败判据：401/403 或 `APIG.0602`（SecurityToken 过期/被吊销）。"""
    assert ca.is_auth_error(401, "") is True
    assert ca.is_auth_error(403, "") is True
    assert ca.is_auth_error(400, '{"error_code":"APIG.0602","error_msg":"Invalid token"}') is True
    assert ca.is_auth_error(400, "invalid token") is True
    assert ca.is_auth_error(400, "model not registered") is False


def test_http_error_code_vocabulary():
    assert ca.http_error_code(401, "") == "AUTH"
    assert ca.http_error_code(429, "") == "RATE_LIMIT"
    assert ca.http_error_code(400, '{"error":"context window exceeded"}') == "CONTEXT_WINDOW_EXCEEDED"
    assert ca.http_error_code(400, "bad params") == "INVALID_REQUEST"
    assert ca.http_error_code(503, "") == "SERVER"


def test_transport_error_detection():
    """网关 ~60s 空闲断连的症状（`terminated` / socket reset）→ 可重试 TRANSPORT。"""
    assert ca.is_transport_error(RuntimeError("TypeError: terminated")) is True
    assert ca.is_transport_error(RuntimeError("socket hang up")) is True
    assert ca.is_transport_error(RuntimeError("ECONNRESET")) is True
    assert ca.is_transport_error(ValueError("bad json")) is False


# ══ 9. 模型列表 ══════════════════════════════════════

def test_normalize_model_id_strips_date_suffix():
    """⚠️ 去掉末尾 `-NNNN` 日期后缀。

    远端目录下发 `deepseek-v4-flash-0731`，但 chat 端点只认无后缀 id
    （带上报 InferHub.002002009.404 "The model is not registered"）。
    只匹配恰好 4 位数字 —— `glm-5.3-flash` 这类不能被误改。
    """
    assert ca.normalize_model_id("deepseek-v4-flash-0731") == "deepseek-v4-flash"
    assert ca.normalize_model_id("glm-5.3-flash") == "glm-5.3-flash"
    assert ca.normalize_model_id("GLM-5.2") == "GLM-5.2"
    assert ca.normalize_model_id("openpangu-2.0-flash-123") == "openpangu-2.0-flash-123"  # 3 位不去
    assert ca.normalize_model_id("model-12345") == "model-12345"                          # 5 位不去
    assert ca.normalize_model_id("") == ""


def test_vl_models_filtered():
    """`-VL-` 中间 / `-VL` 结尾的视觉模型从列表隐藏（上下文小、不支持工具调用）。

    对齐 `src/models.ts:58` 的判据：`id.includes('-VL-') || id.endsWith('-VL')`。
    注释里点名的例子 `Qwen3-VL-235B` 正是命中 `-VL-` 的那个。
    """
    assert ca.is_vl_model("Qwen3-VL-235B") is True
    assert ca.is_vl_model("Qwen3-VL-235B-A22B") is True
    assert ca.is_vl_model("some-VL-model") is True
    assert ca.is_vl_model("Qwen3-VL") is True
    assert ca.is_vl_model("glm-5.2-vl") is True
    # 不含 -VL- 也不以 -VL 结尾的不能被误伤
    assert ca.is_vl_model("GLM-5.2") is False
    assert ca.is_vl_model("GLM-4.5V") is False
    assert ca.is_vl_model("deepseek-v4-flash") is False
    assert ca.is_vl_model("openpangu-2.0-pro") is False


def test_parse_gateway_models_shape():
    """opengw 响应是 `result.models`（不是统一信封的 data）。"""
    body = {"result": {"models": [
        {"model_id": "glm-5.3-flash-0801", "model_name": "glm-5.3-flash"},
        {"model_id": "Qwen3-VL", "model_name": "Qwen3-VL"},
        {"model_id": "glm-5.3-flash-0801", "model_name": "dup"},
    ]}}
    out = ca.parse_gateway_models(body)
    assert out == [{"id": "glm-5.3-flash", "name": "glm-5.3-flash"}]   # 去重 + 过滤 VL


def test_parse_builtin_models_shape():
    """snap-access `/v1/model/builtin` 响应是顶层 `builtinModels`。"""
    body = {"count": 2, "builtinModels": [
        {"model_id": "GLM-5.2", "model_name": "GLM-5.2"},
        {"model_id": "openpangu-2.0-flash", "model_name": "openpangu-2.0-flash"},
    ]}
    out = ca.parse_builtin_models(body)
    assert [m["id"] for m in out] == ["GLM-5.2", "openpangu-2.0-flash"]
    assert ca.parse_builtin_models({}) == []
    assert ca.parse_builtin_models(None) == []


def test_merge_models_first_wins():
    """双端点合并：**先到先得**（gateway 在前，其名字优先）。"""
    a = [{"id": "m1", "name": "gateway-m1"}, {"id": "m2", "name": "gateway-m2"}]
    b = [{"id": "m2", "name": "builtin-m2"}, {"id": "m3", "name": "builtin-m3"}]
    out = ca.merge_models(a, b)
    assert [m["id"] for m in out] == ["m1", "m2", "m3"]
    assert out[1]["name"] == "gateway-m2"
    assert ca.merge_models([], []) == []


def test_parse_model_entry_rejects_bad_entries():
    seen = set()
    assert ca.parse_model_entry({"model_id": ""}, seen) is None
    assert ca.parse_model_entry({"model_id": 123}, seen) is None
    assert ca.parse_model_entry("str", seen) is None
    e = ca.parse_model_entry({"model_id": "m1"}, seen)
    assert e == {"id": "m1", "name": "m1"}          # 名字缺失时用 id 兜底
    assert ca.parse_model_entry({"model_id": "m1"}, seen) is None   # 去重


def test_fetch_signed_get_adds_unsigned_headers_after_signing(monkeypatch):
    """⚠️ `Agent-Type` / `X-Language` 必须**签名后**追加。

    一旦它进 canonical request 与 SignedHeaders，服务端回
    401 APIG.0301 "verify ak sk signature fail"（实测，2026-09-18）。
    """
    calls = []

    class _Resp:
        status_code = 200
        text = '{"count":0,"builtinModels":[]}'

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls.append({"url": url, "headers": headers or {}})
            return _Resp()

    monkeypatch.setattr(ca.httpx, "AsyncClient", _Client)
    text = _run(ca.fetch_signed_get(
        "https://snap-access.cn-north-4.myhuaweicloud.com/v1/model/builtin",
        "AK", "SK", "ST", extra_unsigned_headers=dict(ca.SNAP_UNSIGNED_HEADERS)))
    assert text is not None
    h = calls[0]["headers"]
    assert h["Agent-Type"] == "PromptCenter"
    assert "agent-type" not in h["Authorization"].lower()
    assert h["Authorization"].startswith("SDK-HMAC-SHA256 Access=AK,")


def test_fetch_signed_get_returns_none_without_ak():
    """缺 AK/SK 时直接返回 None（不发一个必然 401 的请求）。"""
    assert _run(ca.fetch_signed_get("https://x.cn/a", "", "SK", "ST")) is None
    assert _run(ca.fetch_signed_get("https://x.cn/a", "AK", "", "ST")) is None


def test_static_seed_matches_jet_hub_defaults():
    """静态兜底种子与 Jet-Hub 的 DEFAULT_MODELS 完全一致（无日期后缀）。"""
    ids = [m["id"] for m in ca.static_model_seed()]
    assert ids == ["GLM-5.2", "GLM-5.1", "GLM-5", "glm-5.3-flash",
                   "openpangu-2.0-flash", "openpangu-2.0-pro",
                   "deepseek-v4-flash", "deepseek-v4-pro"]
    assert all(not ca.normalize_model_id(i) != i for i in ids)


def test_context_windows_table():
    """上下文窗口（未公开的模型留 None 让后端裁剪，不编造数字）。"""
    assert ca.model_context_window("GLM-5.2") == 202752
    assert ca.model_context_window("glm-5.3-flash") == 1048576
    assert ca.model_context_window("unknown-model") is None


# ══ 10. 额度 ═════════════════════════════════════════

def _patch_signed(monkeypatch, results, calls=None):
    """伪造 snap-access 的签名 GET/POST（结果按调用顺序弹出）。"""
    results = list(results)
    calls = calls if calls is not None else []

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls.append({"m": "GET", "url": url, "headers": headers or {}})
            return _Resp(*results.pop(0)) if results else _Resp(200, {})

        async def post(self, url, headers=None, content=None):
            calls.append({"m": "POST", "url": url, "headers": headers or {},
                          "content": content})
            return _Resp(*results.pop(0)) if results else _Resp(200, {})

    monkeypatch.setattr(ca.httpx, "AsyncClient", _Client)
    return calls


def _account_payload(credit=True, token=False, metrics=None):
    return {
        "package": {"is_credit_package": credit, "is_token_package": token,
                    "spec_code": "codearts.agent.enterprise",
                    "package_name_cn": "企业版", "package_name_en": "Enterprise",
                    "status": "normal"},
        "metrics": metrics if metrics is not None else [
            {"name": "usageTotalPackageCredit", "package_credit_amount": 6000,
             "package_credit_used": 1000, "package_credit_remain": 5000},
            {"name": "usageBasicPackageCredit", "package_credit_amount": 3000,
             "package_credit_used": 0, "package_credit_remain": 3000},
            {"name": "usageBonusPackageCredit", "package_credit_amount": 3000,
             "package_credit_used": 1000, "package_credit_remain": 2000},
        ],
    }


def test_parse_credit_remain_does_not_sum_categories():
    """⚠️ 取 `usageTotalPackageCredit.package_credit_remain`，**不累加分类**。

    分类（基础/按需/赠送）是总额的构成明细：相加会重复计算
    （本例 5000 会被算成 10000）。
    """
    metrics = _account_payload()["metrics"]
    assert ca.parse_credit_remain(metrics) == 5000.0


def test_parse_credit_remain_none_vs_zero():
    """⚠️ `None`（无积分口径）与 `0.0`（有口径但余额 0）必须严格区分。

    把前者当 0 会让 Token 账户显示成「积分已用完」，把用户引向错误排查方向。
    """
    assert ca.parse_credit_remain(None) is None
    assert ca.parse_credit_remain([]) is None
    assert ca.parse_credit_remain([{"name": "other", "package_credit_remain": 9}]) is None
    zero = [{"name": "usageTotalPackageCredit", "package_credit_remain": 0}]
    assert ca.parse_credit_remain(zero) == 0.0


def test_parse_credit_remain_falls_back_to_sum_only_without_total():
    """总额 metric 缺失时才回退分类求和。"""
    only_cats = [
        {"name": "usageBasicPackageCredit", "package_credit_remain": 10},
        {"name": "usageBonusPackageCredit", "package_credit_remain": 5},
    ]
    assert ca.parse_credit_remain(only_cats) == 15.0


def test_fetch_account_info_parses_and_flags(monkeypatch):
    """账户类型检测：`is_credit_package` 是能否领积分的前置条件。"""
    _patch_signed(monkeypatch, [(200, _account_payload())])
    info, err = _run(ca.fetch_account_info({"access_key_id": "AK", "secret_access_key": "SK",
                                            "security_token": "ST"}))
    assert err == ""
    assert info["is_credit_package"] is True
    assert info["is_token_package"] is False
    assert info["package_name"] == "企业版"
    assert info["credit_remain"] == 5000.0


def test_fetch_account_info_requires_ak_sk():
    info, err = _run(ca.fetch_account_info({"access_key_id": "", "secret_access_key": ""}))
    assert info is None and "AK/SK" in err


def test_fetch_account_info_http_error_keeps_upstream_reason(monkeypatch):
    """HTTP 错误必须带出服务端原因（APIG.0301 与「凭据过期」处置方式完全不同）。"""
    _patch_signed(monkeypatch, [(401, {"error_code": "APIG.0301",
                                       "error_msg": "verify ak sk signature fail"})])
    info, err = _run(ca.fetch_account_info({"access_key_id": "AK",
                                            "secret_access_key": "SK"}))
    assert info is None
    assert "APIG.0301" in err and "401" in err


# ══ 11. 签到四步 ═════════════════════════════════════

def _delivery_payload(campaign_id=1, claimable=True, status="ENTRY",
                      amount=1000, type_="USER_LOGIN"):
    return {"code": 0, "data": {"items": [
        {"campaignId": campaign_id, "type": type_, "title": "每日签到得积分",
         "claimable": claimable, "status": status, "benefitAmount": amount},
        {"campaignId": 99, "type": "INVITE_USER", "title": "邀请好友",
         "claimable": True, "status": "ENTRY", "benefitAmount": 500},
    ]}}


def _cred():
    return {"access_key_id": "AK", "secret_access_key": "SK", "security_token": "ST"}


def test_checkin_happy_path_four_steps(monkeypatch):
    """四步顺序：账户类型 → /v1/ops/delivery → /v1/ops/claim → /v1/ops/confirm。"""
    calls = _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, _delivery_payload()),
        (200, {"code": 0, "data": {"id": 7788, "benefitAmount": 1000}}),
        (200, {"code": 0, "data": {"ok": True}}),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "claimed"
    assert out["credit"] == 1000.0
    urls = [c["url"] for c in calls]
    assert urls[0].endswith("/snap-manager/v1/statistics/plugin")
    assert urls[1].endswith("/v1/ops/delivery?channel=IDE")
    assert urls[2].endswith("/v1/ops/claim")
    assert urls[3].endswith("/v1/ops/confirm")
    # claim body 回传 campaignId（数字型也转成字符串回传）
    assert json.loads(calls[2]["content"]) == {"campaignId": "1", "channel": "IDE"}
    assert json.loads(calls[3]["content"]) == {"campaignId": "1"}


def test_checkin_numeric_campaign_id(monkeypatch):
    """⚠️ 坑 1：`campaignId` 是**数字型**（实测 1）—— 用只接受字符串的读法会
    得到空串，领取被判「活动缺少 campaignId」而失败（用户看到「1 个失败」
    但积分其实没领到）。"""
    assert ca.read_identifier({"campaignId": 1}, "campaignId") == "1"
    assert ca.read_identifier({"campaignId": "2"}, "campaignId") == "2"
    assert ca.read_identifier({"campaignId": None}, "campaignId") == ""
    assert ca.read_identifier({"campaignId": [1]}, "campaignId") == ""
    calls = _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, _delivery_payload(campaign_id=42)),
        (200, {"code": 0, "data": {"id": None, "benefitAmount": 1000}}),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "claimed"
    assert json.loads(calls[2]["content"])["campaignId"] == "42"


def test_checkin_skips_confirm_when_id_is_null(monkeypatch):
    """⚠️ 坑 4（补 confirm 的判据）：响应 `id === null` 时**不**发 confirm。

    IDE 的判据就是 `benefit.id !== null`；无条件补发会给服务端多打一次请求。
    """
    calls = _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, _delivery_payload()),
        (200, {"code": 0, "data": {"id": None, "benefitAmount": 1000}}),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "claimed"
    assert len(calls) == 3
    assert not any(c["url"].endswith("/v1/ops/confirm") for c in calls)


def test_checkin_amount_field_is_benefit_amount(monkeypatch):
    """⚠️ 坑 2：可领积分字段名是 **`benefitAmount`**（实测 1000），不是 `amount`。

    读错字段会回退成 0，UI 显示「+0 积分」。
    """
    act = ca.parse_activity({"campaignId": 1, "type": "USER_LOGIN",
                             "claimable": True, "status": "ENTRY",
                             "benefitAmount": 1000})
    assert act["amount"] == 1000.0
    # amount 作为兼容回退仍然可用
    assert ca.parse_activity({"benefitAmount": 0, "amount": 50})["amount"] == 50.0
    # 都没有 → 0（不臆造 1000）
    assert ca.parse_activity({})["amount"] == 0.0


def test_checkin_status_null_is_tolerated(monkeypatch):
    """⚠️ 坑 3：`status` 可能为 **null**（不可领取时）。

    解析必须容忍（统一成空串），否则 None 参与比较/拼接会抛异常；
    且此时既不算 already_claimed 也不算失败，而是 inactive。
    """
    act = ca.parse_activity({"campaignId": 1, "type": "USER_LOGIN",
                             "claimable": False, "status": None})
    assert act["status"] == ""
    assert act["claimable"] is False
    calls = _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, _delivery_payload(claimable=False, status=None)),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "inactive"
    assert "status=" in out["message"]
    assert len(calls) == 2                    # 没发 claim


def test_checkin_token_account_is_inactive_not_failed(monkeypatch):
    """Token 计费账户 → `inactive`（**不是** failed）。

    活动范围限定「已升级到积分计费模式的用户」，把 Token 账户报成
    「领取失败」会让用户以为系统坏了、反复点击。
    """
    calls = _patch_signed(monkeypatch, [(200, _account_payload(credit=False, token=True))])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "inactive"
    assert "Token 计费账户" in out["message"]
    assert len(calls) == 1                    # 后续步骤全跳过


def test_checkin_already_claimed_is_idempotent(monkeypatch):
    """不可领取 + status 属已领取态 → `already_claimed`（唯一的幂等保护）。

    本协议没有幂等键、也没有「今天已签到」业务码，预检不能省。
    """
    calls = _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, _delivery_payload(claimable=False, status="CLAIMED")),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "already_claimed"
    assert len(calls) == 2
    for st in ("CLAIMED", "CONFIRMED", "CONSUMED"):
        assert st in ca.CLAIMED_STATUSES


def test_checkin_no_user_login_activity_is_inactive(monkeypatch):
    """没有 USER_LOGIN 活动 → inactive（邀请/新人/学生认证不算每日签到）。"""
    _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, {"code": 0, "data": {"items": [
            {"campaignId": 5, "type": "INVITE_USER", "claimable": True,
             "status": "ENTRY", "benefitAmount": 500}]}}),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "inactive"
    assert "每日签到" in out["message"]


def test_checkin_missing_campaign_id_fails(monkeypatch):
    _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, {"code": 0, "data": {"items": [
            {"campaignId": None, "type": "USER_LOGIN", "claimable": True,
             "status": "ENTRY", "benefitAmount": 1000}]}}),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "failed"
    assert "campaignId" in out["message"]


def test_checkin_confirm_failure_still_reports_claimed(monkeypatch):
    """confirm 失败**不**把整体判为失败：积分已在待确认态，报 failed 会让用户
    重复点击；问题留在 error 字段里。"""
    _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, _delivery_payload()),
        (200, {"code": 0, "data": {"id": 1, "benefitAmount": 1000}}),
        (500, {"error_code": "APIG.9999", "error_msg": "server busy"}),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "claimed"
    assert out["credit"] == 1000.0
    assert "confirm" in out["error"]


def test_checkin_credit_falls_back_to_activity_amount(monkeypatch):
    """领取响应不带积分字段时回退活动条目的 benefitAmount；两处都没有则如实 0
    （文档里的 1000 是活动规则，不是本次发放的实测值）。"""
    _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, _delivery_payload(amount=700)),
        (200, {"code": 0, "data": {"id": None}}),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["credit"] == 700.0
    # 活动条目也没有 amount
    _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, _delivery_payload(amount=0)),
        (200, {"code": 0, "data": {"id": None}}),
    ])
    assert _run(ca.claim_daily_checkin(_cred()))["credit"] == 0.0


def test_checkin_business_code_nonzero_is_failure(monkeypatch):
    """HTTP 200 + `code != 0` 是业务失败 —— 不能当成功（否则「活动未开始」
    会被误报成「领取成功」）。"""
    _patch_signed(monkeypatch, [
        (200, _account_payload()),
        (200, {"code": 40001, "message": "活动已结束", "data": None}),
    ])
    out = _run(ca.claim_daily_checkin(_cred()))
    assert out["kind"] == "failed"
    assert "活动已结束" in out["message"]


# ══ 12. 适配器 ═══════════════════════════════════════

def _api_key(ak="AK", sk="SK", st="ST"):
    return json.dumps({"access_key_id": ak, "secret_access_key": sk,
                       "security_token": st,
                       "expires_at": "2099-01-01T00:00:00Z"})


def test_adapter_rejects_bare_token():
    """裸 token（非 JSON）必须给出明确说明 —— 静默当成 AK 用只会得到一个
    看不出根因的验签 401。"""
    a = CodeArtsAdapter()
    cred = a._resolve_credentials("dt-some-token")
    assert cred["_error"] and "JSON" in cred["_error"]
    cred2 = a._resolve_credentials('{"access_key_id":"AK"}')
    assert cred2["_error"] and "SK" in cred2["_error"]
    assert a._resolve_credentials("")["_error"]
    assert a._resolve_credentials("{bad json")["_error"]
    assert "_error" not in a._resolve_credentials(_api_key())


def test_adapter_stream_emits_openai_chunks(monkeypatch):
    """流式：content / reasoning_content / finish 帧都要转成 OpenAI chunk。

    reasoning_content 通道**全部**按 reasoning 输出（含其中无 <thought> 标签的
    部分）—— 否则 deepseek-v4 的思考会泄漏到正文（实测 2026-08-22）。
    """
    lines = [
        'data: {"choices":[{"delta":{"content":"你好"}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"我在思考"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    calls = _patch_stream(monkeypatch, lines)
    a = CodeArtsAdapter()
    req = _req(model="GLM-5.2")
    chunks = _run(_collect(a.stream_chat_completion(req, _api_key(), "")))
    text = "".join(c["choices"][0]["delta"].get("content") or ""
                   for c in chunks if c.get("choices"))
    reasoning = "".join(c["choices"][0]["delta"].get("reasoning_content") or ""
                        for c in chunks if c.get("choices"))
    assert text == "你好"
    assert reasoning == "我在思考"
    finishes = [c["choices"][0].get("finish_reason") for c in chunks if c.get("choices")]
    assert "stop" in finishes
    # 签名头齐备 + maas_type 不在（GLM-5.2 不是 benefit 模型）
    h = calls[0]["headers"]
    assert h["Authorization"].startswith("SDK-HMAC-SHA256 Access=AK,")
    assert "maas_type" not in h
    assert h["Content-Type"] == "application/json"
    assert h["Accept"] == "text/event-stream"
    assert h["Accept-Encoding"] == "identity"


def test_adapter_stream_signs_maas_type_for_benefit_model(monkeypatch):
    """`glm-5.3-flash` 的请求必须带 maas_type: benefit **且参与签名**。

    否则后端报 InferHub.002002009.404 "model is not registered" —— 看起来像
    模型不存在，其实是少了这个头。
    """
    lines = ['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"]
    calls = _patch_stream(monkeypatch, lines)
    a = CodeArtsAdapter()
    req = _req(model="glm-5.3-flash")
    _run(_collect(a.stream_chat_completion(req, _api_key(), "")))
    h = calls[0]["headers"]
    assert h["maas_type"] == "benefit"
    assert "maas_type;x-sdk-content-sha256" in h["Authorization"]   # 参与签名


def test_adapter_dsml_mode_omits_tools_field(monkeypatch):
    """deepseek-v4 + 有工具 → 请求体**不含 tools**，schema 进 system 消息。"""
    lines = ['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"]
    calls = _patch_stream(monkeypatch, lines)
    a = CodeArtsAdapter()
    tools = [{"type": "function", "function": {"name": "write",
                                               "parameters": {"type": "object"}}}]
    req = _req(model="deepseek-v4-flash", tools=tools)
    _run(_collect(a.stream_chat_completion(req, _api_key(), "")))
    body = json.loads(calls[0]["content"])
    assert "tools" not in body
    assert any(m["role"] == "system" and "DSML" in m["content"]
               for m in body["messages"])


def test_adapter_non_dsml_model_keeps_tools(monkeypatch):
    """GLM-5.2 + 工具 → 保留标准 tools 字段（它不经 APIG 网关，无断连问题）。"""
    lines = ['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"]
    calls = _patch_stream(monkeypatch, lines)
    a = CodeArtsAdapter()
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
    req = _req(model="GLM-5.2", tools=tools)
    _run(_collect(a.stream_chat_completion(req, _api_key(), "")))
    body = json.loads(calls[0]["content"])
    assert body["tools"] == tools


def test_adapter_parses_dsml_tool_calls_from_content(monkeypatch):
    """DSML 块从 `delta.content` 到达 → 转成结构化 tool_calls（不是正文）。"""
    block = _dsml_block(name="write", param="content", value="hello")
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": block}}]}),
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    _patch_stream(monkeypatch, lines)
    a = CodeArtsAdapter()
    chunks = _run(_collect(a.stream_chat_completion(
        _req(model="deepseek-v4-flash"), _api_key(), "")))
    # 原始 DSML 标签不能泄漏到正文
    text = "".join(c["choices"][0]["delta"].get("content") or ""
                   for c in chunks if c.get("choices"))
    assert "DSML" not in text
    tool_names, tool_args = _tool_calls_of(chunks)
    assert tool_names == ["write"]
    assert json.loads(tool_args[0]) == {"content": "hello"}
    finishes = [c["choices"][0].get("finish_reason") for c in chunks
                if c.get("choices") and c["choices"][0].get("finish_reason")]
    assert finishes[-1] == "tool_calls"


def test_adapter_thought_in_content_goes_to_reasoning(monkeypatch):
    """content 里的 `<thought>` 内容分流到 reasoning，不进正文。"""
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "<thought>想一下</thought>答案"}}]}),
        "data: [DONE]",
    ]
    _patch_stream(monkeypatch, lines)
    chunks = _run(_collect(CodeArtsAdapter().stream_chat_completion(
        _req(model="deepseek-v4-pro"), _api_key(), "")))
    text = "".join(c["choices"][0]["delta"].get("content") or ""
                   for c in chunks if c.get("choices"))
    reasoning = "".join(c["choices"][0]["delta"].get("reasoning_content") or ""
                        for c in chunks if c.get("choices"))
    assert text == "答案"
    assert reasoning == "想一下"


def test_adapter_sse_embedded_queue_error_triggers_retry(monkeypatch):
    """HTTP 200 + SSE 内嵌 `InferHub.ModelArts.81111.429` → 触发排队重试。

    不识别它会把流当正常结束，用户看到「思考后无输出」。
    """
    lines = [
        "data: " + json.dumps({"text": "[DONE]", "error_code": "InferHub.ModelArts.81111.429",
                               "error_msg": "TPM limit"}),
    ]
    calls = _patch_stream(monkeypatch, lines, repeat=True)
    # 缩短队列延时，避免测试真的等 10 秒
    monkeypatch.setattr(ca, "QUEUE_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(ca, "QUEUE_MAX_ATTEMPTS", 2)
    a = CodeArtsAdapter()
    chunks = _run(_collect(a.stream_chat_completion(
        _req(model="GLM-5.2"), _api_key(), "")))
    errs = [c for c in chunks if c.get("error")]
    assert errs and "30 minutes" in errs[0]["error"]
    # 至少发了两次 chat 请求（说明发生了重试）
    streams = [c for c in calls if c["m"] == "STREAM"]
    assert len(streams) >= 2
    assert len(streams) <= 3          # 上限被测试锚定，不会真跑 180 次


def test_adapter_sse_embedded_fatal_error_not_retried(monkeypatch):
    """不可重试的流内错误（如模型未注册）→ 立即报错，不进 30 分钟排队。"""
    lines = ["data: " + json.dumps({"error_code": "InferHub.002002009.404",
                                    "error_msg": "model is not registered"})]
    calls = _patch_stream(monkeypatch, lines, repeat=True)
    chunks = _run(_collect(CodeArtsAdapter().stream_chat_completion(
        _req(model="GLM-5.2"), _api_key(), "")))
    assert any(c.get("error") and "not registered" in c["error"] for c in chunks)
    assert len([c for c in calls if c["m"] == "STREAM"]) == 1


def test_adapter_http_400_non_queue_error_raises_immediately(monkeypatch):
    """400 但不是排队码 → 立即报错（不能拖成 30 分钟超时）。"""
    calls = _patch_stream(monkeypatch, [], status=400, repeat=True)
    chunks = _run(_collect(CodeArtsAdapter().stream_chat_completion(
        _req(model="GLM-5.2"), _api_key(), "")))
    assert any(c.get("error") and "400" in c["error"] for c in chunks)
    assert len([c for c in calls if c["m"] == "STREAM"]) >= 1


def test_adapter_queue_retry_succeeds_second_attempt(monkeypatch):
    """排队重试的**成功**路径：第一次 400 并发超限 → 探到 waiting → 重试即成功。

    顺带锁死：每次重试前都查一次排队状态端点（对齐参考实现）。
    """
    calls = _patch_queue_retry(monkeypatch, fail_times=1)
    monkeypatch.setattr(ca, "QUEUE_RETRY_DELAY_SECONDS", 0.0)
    chunks = _run(_collect(CodeArtsAdapter().stream_chat_completion(
        _req(model="GLM-5.2"), _api_key(), "")))
    text = "".join(c["choices"][0]["delta"].get("content") or ""
                   for c in chunks if c.get("choices"))
    assert text == "done"
    assert len([c for c in calls if c["m"] == "STREAM"]) == 2
    assert len([c for c in calls if c["m"] == "GET"]) >= 1
    # 排队状态查询必须带签名（与 chat 同一套 AK/SK）且不带 content-type
    probe = [c for c in calls if c["m"] == "GET"][0]
    assert probe["headers"]["Authorization"].startswith("SDK-HMAC-SHA256 Access=AK,")
    assert "content-type" not in probe["headers"]
    assert probe["headers"]["Agent-Type"] == "INFERHUB_AGENT"


def test_adapter_queue_full_is_terminal(monkeypatch):
    """排队状态端点为终态（`queue_full` / `error`）→ **立即**报错。

    不短路的话用户要等满 30 分钟才收到一个本来此刻就能确定的失败。
    """
    calls = _patch_queue_retry(monkeypatch, fail_times=99, queue_status="queue_full")
    # 上限改小：短路被删掉时本用例快速失败而不是挂住
    monkeypatch.setattr(ca, "QUEUE_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(ca, "QUEUE_MAX_ATTEMPTS", 3)
    chunks = _run(_collect(CodeArtsAdapter().stream_chat_completion(
        _req(model="GLM-5.2"), _api_key(), "")))
    errs = [c["error"] for c in chunks if c.get("error")]
    assert errs and "队列已满" in errs[0]
    assert len([c for c in calls if c["m"] == "STREAM"]) == 1   # 不再重试


def test_adapter_queue_probe_unreachable_fails_fast(monkeypatch):
    """⚠️ 状态端点不可达 + **非**排队码 → 按原错误立即抛出。

    这是「不要把所有 400 都拖成 30 分钟超时」的守护：只有状态端点明确回报
    waiting/queue_full 才进排队流程。
    """
    calls = _patch_queue_retry(monkeypatch, fail_times=99, non_queue_error=True,
                               probe_raises=True)
    # 把延时与上限改小：万一守护被删掉，本用例**快速失败**而不是挂满 30 分钟
    # （挂住比失败更糟 —— 它会把整个测试套拖死，看起来像环境问题）。
    monkeypatch.setattr(ca, "QUEUE_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(ca, "QUEUE_MAX_ATTEMPTS", 3)
    chunks = _run(_collect(CodeArtsAdapter().stream_chat_completion(
        _req(model="GLM-5.2"), _api_key(), "")))
    errs = [c["error"] for c in chunks if c.get("error")]
    assert errs and "HTTP 400" in errs[0] and "30 minutes" not in errs[0]
    assert len([c for c in calls if c["m"] == "STREAM"]) == 1   # 不重试
    assert len([c for c in calls if c["m"] == "GET"]) == 1      # 只探一次


def test_adapter_auth_error_triggers_refresh_callback(monkeypatch):
    """401 / APIG.0602 → 调续期回调后用新凭据重试一次（覆盖「后端提前吊销」）。"""
    lines = ['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"]
    calls = _patch_stream(monkeypatch, lines, status=401, fail_first=True)
    refreshed = {"called": 0}

    async def _refresh():
        refreshed["called"] += 1
        return _api_key(ak="AK2", sk="SK2", st="ST2")

    a = CodeArtsAdapter()
    chunks = _run(_collect(a.stream_chat_completion(
        _req(model="GLM-5.2"), _api_key(), "",
        extra_headers={"__codearts_refresh": _refresh})))
    assert refreshed["called"] == 1
    text = "".join(c["choices"][0]["delta"].get("content") or ""
                   for c in chunks if c.get("choices"))
    assert text == "ok"
    # 第二次请求用新 AK 签名
    second = [c for c in calls if c["m"] == "STREAM"][1]
    assert "Access=AK2," in second["headers"]["Authorization"]


def test_adapter_auth_error_without_callback_fails_clearly(monkeypatch):
    _patch_stream(monkeypatch, [], status=401)
    chunks = _run(_collect(CodeArtsAdapter().stream_chat_completion(
        _req(model="GLM-5.2"), _api_key(), "")))
    assert any(c.get("error") and "鉴权失败" in c["error"] for c in chunks)


def test_adapter_transport_error_reported_as_retryable(monkeypatch):
    """网关空闲断连（`terminated`）→ 报 transport error（明确可重试语义）。"""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, content=None):
            raise RuntimeError("TypeError: terminated")

    monkeypatch.setattr(ad_mod.httpx, "AsyncClient", _Client)
    chunks = _run(_collect(CodeArtsAdapter().stream_chat_completion(
        _req(model="deepseek-v4-flash"), _api_key(), "")))
    assert any(c.get("error") and "transport" in c["error"] for c in chunks)


def test_adapter_chat_completion_aggregates_stream(monkeypatch):
    """非流式：聚合自身 SSE（上游只有流式），usage/tool_calls 都要带上。"""
    block = _dsml_block(name="read", param="path", value="a.py")
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}',
        "data: " + json.dumps({"choices": [{"delta": {"content": block}}]}),
        "data: " + json.dumps({"choices": [{"delta": {}}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                         "total_tokens": 15}}),
        "data: [DONE]",
    ]
    _patch_stream(monkeypatch, lines)
    out = _run(CodeArtsAdapter().chat_completion(
        _req(model="deepseek-v4-flash"), _api_key(), ""))
    assert out["object"] == "chat.completion"
    msg = out["choices"][0]["message"]
    assert msg["reasoning_content"] == "想"
    assert msg["tool_calls"][0]["function"]["name"] == "read"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}
    assert out["usage"]["total_tokens"] == 15


def test_adapter_chat_completion_raises_on_error_chunk(monkeypatch):
    lines = ["data: " + json.dumps({"error_code": "InferHub.002002009.404",
                                    "error_msg": "model is not registered"})]
    _patch_stream(monkeypatch, lines)
    with pytest.raises(RuntimeError) as e:
        _run(CodeArtsAdapter().chat_completion(_req(model="GLM-5.2"),
                                               _api_key(), ""))
    assert "not registered" in str(e.value)


def test_adapter_list_models_online_merges_and_filters(monkeypatch):
    """在线目录：双端点合并 + 去日期后缀 + 过滤 VL + 标注 benefit 免费。"""
    calls = _patch_signed(monkeypatch, [
        (200, {"result": {"models": [
            {"model_id": "glm-5.3-flash-0801", "model_name": "glm-5.3-flash"}]}}),
        (200, {"builtinModels": [
            {"model_id": "GLM-5.2", "model_name": "GLM-5.2"},
            {"model_id": "Qwen3-VL", "model_name": "Qwen3-VL"},
            {"model_id": "deepseek-v4-flash-0731", "model_name": "DeepSeek V4 Flash"}]}),
    ])
    models = _run(CodeArtsAdapter().list_models(_api_key(), ""))
    ids = [m.model_id for m in models]
    assert ids == ["glm-5.3-flash", "GLM-5.2", "deepseek-v4-flash"]
    by_id = {m.model_id: m for m in models}
    assert by_id["glm-5.3-flash"].is_free is True          # benefit 模型免额度
    assert by_id["GLM-5.2"].is_free is False
    assert by_id["deepseek-v4-flash"].context_length == 1048576
    assert by_id["deepseek-v4-flash"].display_name == "DeepSeek V4 Flash"
    assert all(m.supports_vision is False for m in models)
    assert all(m.input_modalities == ["text"] for m in models)


def test_adapter_list_models_falls_back_to_static_seed(monkeypatch):
    """在线目录失败（但凭据可用）→ 回退静态种子（绝不能返回空列表）。"""
    _patch_signed(monkeypatch, [(500, {}), (500, {})])
    models = _run(CodeArtsAdapter().list_models(_api_key(), ""))
    assert [m.model_id for m in models] == list(ca.DEFAULT_MODELS)
    # 即使上游全挂也不抛异常
    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(ca.httpx, "AsyncClient", _Boom)
    assert _run(CodeArtsAdapter().list_models(_api_key(), "")) != []


def test_adapter_list_models_empty_when_credential_invalid():
    """凭据不可用 → 空列表（不冒充有模型），且不抛异常。"""
    assert _run(CodeArtsAdapter().list_models("bare-token", "")) == []


def test_adapter_health_check_states(monkeypatch):
    """探活：目录拉到且有该模型 → healthy；目录空 → degraded（不谎报 healthy）。"""
    _patch_signed(monkeypatch, [
        (200, {"result": {"models": [{"model_id": "GLM-5.2"}]}}),
        (200, {"builtinModels": []}),
    ])
    a = CodeArtsAdapter()
    r = _run(a.health_check("GLM-5.2", _api_key(), ""))
    assert r.status == "healthy"
    _patch_signed(monkeypatch, [
        (200, {"result": {"models": [{"model_id": "GLM-5.2"}]}}),
        (200, {"builtinModels": []}),
    ])
    assert _run(a.health_check("nope", _api_key(), "")).status == "degraded"
    _patch_signed(monkeypatch, [(500, {}), (500, {})])
    r3 = _run(a.health_check("GLM-5.2", _api_key(), ""))
    assert r3.status == "degraded" and "目录" in r3.error_message
    r4 = _run(a.health_check("GLM-5.2", "bare-token", ""))
    assert r4.status == "unhealthy"


def test_adapter_session_id_is_stable(monkeypatch):
    """`prompt_cache_key` 必须是**稳定**值：随机值会让服务端缓存永远打不中。"""
    lines = ['data: {"choices":[{"delta":{"content":"x"}}]}', "data: [DONE]"]
    calls = _patch_stream(monkeypatch, lines, repeat=True)
    a = CodeArtsAdapter()
    req = _req(model="GLM-5.2")
    _run(_collect(a.stream_chat_completion(req, _api_key(), "")))
    _run(_collect(a.stream_chat_completion(req, _api_key(), "")))
    keys = [json.loads(c["content"])["prompt_cache_key"]
            for c in calls if c["m"] == "STREAM"]
    assert keys[0] and keys[0] == keys[1]
    # 显式传入的会话 id 优先
    calls.clear()
    _run(_collect(a.stream_chat_completion(req, _api_key(), "",
                                           extra_headers={"session_id": "S-9"})))
    assert json.loads(calls[0]["content"])["prompt_cache_key"] == "S-9"


# ── 流式测试脚手架 ──────────────────────────────────
def _patch_stream(monkeypatch, lines, status=200, repeat=False, fail_first=False):
    """伪造 httpx 的 client.stream()；记录 method/url/headers/content。

    `repeat=True` 让同一组 lines 可被多次弹出（用于重试路径）；
    `fail_first=True` 让第一次返回 status（模拟 401），之后返回 200 + lines。
    """
    state = {"count": 0, "failed": False}
    calls = []

    class _Resp:
        def __init__(self, status_code, body_lines, calls_ref):
            self.status_code = status_code
            self._body_lines = body_lines
            self._calls = calls_ref

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aread(self):
            return json.dumps({"error_code": "APIG.0602",
                               "error_msg": "Invalid token"}).encode()

        def json(self):
            """排队状态端点（GET）的响应：统一返回 working，即「未在排队」。"""
            return {"status": "working", "queue_position": -1, "message": ""}

        async def aiter_lines(self):
            for line in self._body_lines:
                yield line

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls.append({"m": "GET", "url": url, "headers": headers or {}})
            return _Resp(200, [], calls)

        def stream(self, method, url, headers=None, content=None):
            calls.append({"m": "STREAM", "url": url, "headers": headers or {},
                          "content": content})
            state["count"] += 1
            if fail_first and state["count"] == 1:
                return _Resp(status, [], calls)
            if status >= 400 and not fail_first:
                return _Resp(status, [], calls)
            return _Resp(200, list(lines), calls)

    monkeypatch.setattr(ad_mod.httpx, "AsyncClient", _Client)
    return calls


def _patch_queue_retry(monkeypatch, fail_times=1, queue_status="waiting",
                       non_queue_error=False, probe_raises=False):
    """伪造「chat 先失败 N 次、状态端点报 queue_status」的场景。

    `non_queue_error=True` 时 chat 返回**非**排队码（不会被自动判定为排队），
    用于验证「探不到排队就快速失败」的守护。
    """
    state = {"stream": 0}
    calls = []

    class _Resp:
        def __init__(self, status, lines=None, jbody=None, body=b""):
            self.status_code = status
            self._lines = lines or []
            self._j = jbody
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aread(self):
            return self._body

        def json(self):
            return self._j

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    err_code = (b'{"error_code":"InferHub.002002009.404",'
                b'"error_msg":"model is not registered"}') if non_queue_error else (
                b'{"error_code":"TM.00001041","error_msg":"concurrency limit reached"}')

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls.append({"m": "GET", "url": url, "headers": headers or {}})
            if probe_raises:
                raise RuntimeError("status endpoint down")
            return _Resp(200, jbody={"status": queue_status,
                                     "queue_position": 1, "message": "队列已满"})

        def stream(self, method, url, headers=None, content=None):
            calls.append({"m": "STREAM", "url": url, "headers": headers or {},
                          "content": content})
            state["stream"] += 1
            if state["stream"] <= fail_times:
                return _Resp(400, body=err_code)
            return _Resp(200, lines=[
                'data: {"choices":[{"delta":{"content":"done"}}]}',
                "data: [DONE]"])

    monkeypatch.setattr(ad_mod.httpx, "AsyncClient", _Client)
    return calls


def _collect(agen):
    async def _inner():
        out = []
        async for c in agen:
            out.append(c)
        return out
    return _inner()


def _tool_calls_of(chunks):
    names, args = [], []
    for c in chunks:
        for choice in (c.get("choices") or []):
            for tc in ((choice.get("delta") or {}).get("tool_calls") or []):
                fn = tc.get("function") or {}
                if fn.get("name"):
                    names.append(fn["name"])
                args.append(fn.get("arguments") or "")
    return names, args
