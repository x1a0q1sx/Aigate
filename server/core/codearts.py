"""CodeArts（华为 DevEco / codearts.huaweicloud.com）协议实现。

协议来源：Jet-Hub 源码逐行核对（github.com/zhengwuji/Jet-Hub，MIT）
+ 其 `tests/e2e/*` 里的真实实测记录（含错误码原文）。本模块只承载
**协议细节**（签名 / 授权 URL / 换票续期 / 模型列表 / 额度 / 签到 / 消息线格式），
登录编排与 DB 读写在 oauth_client（同 lobsterai 的分层）。

## 与其它渠道的差异（全部是实测结论，不是推测）

1. **不是 Bearer，是华为 `SDK-HMAC-SHA256` 请求签名**：每个出站请求用
   AK/SK/SecurityToken 三元组现签（`src/sign.ts`）。无加密、无 WASM、
   无设备码 —— 凭据是浏览器 OAuth（PKCE + DPoP）换来的临时 AK/SK。
2. **授权 URL 的 `code_challenge_method` 是 `SHA-256` 而非标准 `S256`**
   （`src/login.ts:265-267`）：portal 以此识别 OAuth 授权，
   写错会**静默回退旧的 ticket 流程**（用户仍能登录，但拿不到 refresh_token，
   于是永远无法续期）。
3. **回调端口必须 ≥10000**，且主机名被 portal 锁死 `127.0.0.1`
   （`src/login.ts:338-364`）。低端口会被 portal 拒绝。
4. **回调还要支持旧 ticket 流程回退**：收到 `?secret=` 时后台轮询
   `/snap-manager/v1/login/ticket`（`src/login.ts:297-311`）。
   这条回退路径只签发 AK/SK（无 refresh_token）。
5. **token 请求必须带 DPoP ES256 JWS 头**（`src/oauth.ts:74-85`），
   body 是 form-urlencoded（**不是** JSON），响应给 AK/SK/SecurityToken 三元组。
6. ⚠️ **refresh_token 一次性轮换**：`tests/e2e/codearts-credential.ts:9-13`
   记录「用一次即作废，服务端回 `STS5.1806 the refresh token has been used`」，
   该坑在实际开发中已真实踩过。**调用方必须串行化续期**（分布式锁 / 单飞
   per (provider, owner)），否则两个并发刷新会互相作废，用户只能重新登录。
   见 `refresh_token()` 的返回值说明。
7. **请求体的 `messages` 要重写成 CodeArts 方言**（`src/llm-adapter.ts:95-154`）：
   assistant 历史**必须携带 `reasoning_content` 字段**（缺失直接 400），
   `tool_calls.arguments` 必须是 JSON 字符串，孤儿 tool_call / tool_result
   要剔除（否则之后每条消息都 400）。
8. **deepseek-v4 系（flash/pro）必须切 DSML 工具调用模式**：APIG 网关
   ~60s 空闲必断连，标准 `tool_calls` 一次性打包生成大参数会静默数十秒
   → 必然断流。做法是把 tools schema 注入 system 消息、**不发 `tools` 字段**，
   让模型以 `<｜DSML｜tool_calls>` 语法走 `delta.content` 流式输出
   （`src/llm-adapter.ts:156-236`）。
9. **`glm-5.3-flash` 必须带 `maas_type: benefit` 头且该头参与签名**
   （`src/llm-adapter.ts:46-52`）：否则报 `InferHub.002002009.404`
   "model is not registered"（看起来像模型不存在，其实是少了这个头）。
10. **模型 id 要去掉 `-NNNN` 日期后缀**：远端目录下发 `deepseek-v4-flash-0731`，
    但 chat 端点只认 `deepseek-v4-flash`（`src/models.ts:35-49`）。
    `-VL-` / `-VL` 结尾的视觉模型从列表隐藏（上下文小、不支持工具调用）。

## 命名对齐

函数/常量名尽量与 Jet-Hub 同名（`normalize_model_id` / `sign_request_huawei` /
`build_oauth_login_url` …），便于日后双向核对。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import httpx

from server.core.dpop import private_key_from_jwk, public_jwk_from

logger = logging.getLogger(__name__)

# ── 常量（照抄 Jet-Hub src/login.ts / src/oauth.ts / src/models.ts / src/llm-adapter.ts）──

# OAuth 客户端标识（= 其 URI scheme，来自插件 product.json）
CODEARTS_CLIENT_ID = "codearts-agent"
# 本地回调路径（对齐真实插件的 AUTH_REDIRECT_URL）
CODEARTS_REDIRECT_PATH = "/oauth/callback"
# 华为 STS token 端点
CODEARTS_STS_TOKEN_ENDPOINT = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
# portal 授权端点 + 登录结果页
CODEARTS_PORTAL_AUTHORIZE_BASE = "https://codearts.huaweicloud.com/portal/authorize"
CODEARTS_PORTAL_LOGIN_BASE = "https://codearts.huaweicloud.com/portal/login"
# portal 期望的插件名/版本（逆向常量，硬编码为真实扩展版本，勿改）
CODEARTS_LOGIN_PLUGIN_NAME = "snap_AIIDE"
CODEARTS_LOGIN_PLUGIN_VERSION = "5.2.0"
# 主题色 kind（对齐 IDE activeColorTheme.kind：2 = Dark）/ 界面语言
CODEARTS_OAUTH_THEME = "2"
CODEARTS_OAUTH_LOCALE = "zh-cn"

# ⚠️ 非 RFC 标准缩写 S256（见模块 docstring 第 2 条）
CODEARTS_CODE_CHALLENGE_METHOD = "SHA-256"

# 回调端口下限（对齐真实插件：低端口会被 portal 拒绝）
CODEARTS_MIN_CALLBACK_PORT = 10_000
# token 请求超时（与真实插件一致）
CODEARTS_TOKEN_TIMEOUT = 60.0
# OAuth 授权码 / 刷新令牌两种 grant_type
GRANT_AUTHORIZATION_CODE = "authorization_code"
GRANT_REFRESH_TOKEN = "refresh_token"

# snap-access 网关（与 models.ts / codearts-credits.ts 同域）
CODEARTS_SNAP_ACCESS_BASE = "https://snap-access.cn-north-4.myhuaweicloud.com"
CODEARTS_CHAT_PATH = "/api/v2/chat/completions"
CODEARTS_QUEUE_STATUS_PATH = "/api/v1/queue/status"
CODEARTS_MODEL_BUILTIN_PATH = "/v1/model/builtin"
# opengw 网关（benefit 免费额度模型列表）
CODEARTS_OPENGW_GATEWAY_CONFIG_URL = (
    "https://opengw.developer.huaweicloud.com/api/v1/gateway/config"
)
# 账户/套餐 + 签到（snap-manager / ops）
CODEARTS_PACKAGE_INFO_PATH = "/snap-manager/v1/statistics/plugin"
CODEARTS_OPS_DELIVERY_PATH = "/v1/ops/delivery"
CODEARTS_OPS_CLAIM_PATH = "/v1/ops/claim"
CODEARTS_OPS_CONFIRM_PATH = "/v1/ops/confirm"
CODEARTS_OPS_CHANNEL = "IDE"
# 签到活动类型（「每日登录领取」；INVITE_USER / NEW_USER_REGISTER / STUDENT_CERTIFIED 不算）
CODEARTS_DAILY_LOGIN_TYPE = "USER_LOGIN"
# 旧 ticket 流程兜底端点（回调收到 ?secret= 时轮询）
CODEARTS_TICKET_ENDPOINT = (
    "https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager/v1/login/ticket"
)

# 签名后追加、**不参与签名计算**的头（见 codearts-credits.ts:113-133 的实测：
# 一旦 Agent-Type 进入 canonical request，服务端回
# 401 APIG.0301 "verify ak sk signature fail"）
SNAP_UNSIGNED_HEADERS: Dict[str, str] = {
    "Agent-Type": "PromptCenter",
    "X-Language": "zh-cn",
}

# glm-5.3-flash 是 benefit（免费额度）模型：请求必须带 maas_type: benefit
# 且该头**参与签名**，否则 InferHub.002002009.404（见 docstring 第 9 条）
MAAS_TYPE_BENEFIT_MODELS = frozenset({"glm-5.3-flash"})

# 模型上下文窗口（对齐 CodeArts Agent IDE 模型卡标注；未公开的模型留 None 让后端裁剪）
CONTEXT_WINDOWS: Dict[str, int] = {
    "GLM-5.2": 202_752,
    "glm-5.3-flash": 1_048_576,
    "deepseek-v4-flash": 1_048_576,
    "deepseek-v4-pro": 1_048_576,
}

# 在线目录拉不到时的静态种子（照抄 llm-adapter.ts:26-32 的 DEFAULT_MODELS）
DEFAULT_MODELS: Tuple[str, ...] = (
    "GLM-5.2", "GLM-5.1", "GLM-5",
    "glm-5.3-flash",
    "openpangu-2.0-flash", "openpangu-2.0-pro",
    "deepseek-v4-flash", "deepseek-v4-pro",
)

# 并发排队：轮询上限 180 × 10s = 30 分钟（对齐 deveco-code 参考实现）
QUEUE_RETRY_DELAY_SECONDS = 10.0
QUEUE_MAX_ATTEMPTS = 180

# 写文件类大参数工具（仅用于注释说明与诊断，不参与 DSML 判定）
DSML_LARGE_PARAM_TOOLS = ("write", "file_write", "apply_patch")

# 默认输出上限（llm-adapter.ts:918：65536 实测可用，131072 反而触发空流）
DEFAULT_MAX_TOKENS = 65_536

# SSE 首 token / chunk 间超时（对齐 CodeArts Agent IDE agentkernelServer：
# firstTokenTimeout=300s / chunkTimeout=600s；llm-adapter.ts:277-282）
SSE_FIRST_TOKEN_TIMEOUT_SECONDS = 300.0
SSE_CHUNK_TIMEOUT_SECONDS = 600.0

# DSML 标记（全角 ｜ U+FF5C，与模型实际输出一致）
DSML_TOOL_CALLS_OPEN = "<｜DSML｜tool_calls>"
DSML_TOOL_CALLS_CLOSE = "</｜DSML｜tool_calls>"
DSML_INVOKE_OPEN = "<｜DSML｜invoke"
DSML_INVOKE_CLOSE = "</｜DSML｜invoke>"
DSML_PARAM_OPEN = "<｜DSML｜parameter"
DSML_PARAM_CLOSE = "</｜DSML｜parameter>"
THOUGHT_OPEN = "<thought>"
THOUGHT_CLOSE = "</thought>"


# ── 签名（华为 SDK-HMAC-SHA256，逐行移植 src/sign.ts）──────────────

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def sdk_date_stamp(now: Optional[float] = None) -> str:
    """`YYYYMMDDTHHMMSSZ`（UTC）。

    JS 的 `toISOString().replace(/[-:]/g,'').replace(/\\.\\d+Z$/,'Z')` 等价物：
    去掉连字符与冒号、丢掉毫秒（`%Y%m%dT%H%M%SZ` 天然无毫秒）。
    """
    dt = datetime.fromtimestamp(now if now is not None else time.time(),
                                tz=timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def build_canonical_request(method: str, uri: str, query: str,
                            headers: Dict[str, str], payload_hash: str) -> str:
    """canonical request 七段拼接（照抄 `buildCanonicalRequest`）。

    注意第 5 段是**空行**（JS 里 `headerLines.join('\\n')` 后再 `''`）——
    Python 的 `"\\n".join([...])` 写法需要显式留一个空串，否则段数不对。
    """
    signed = sorted(headers.keys())
    header_lines = "\n".join(f"{k}:{headers[k]}" for k in signed)
    return "\n".join([method, uri, query, header_lines, "",
                      ";".join(signed), payload_hash])


def sign_request_huawei(ak: str, sk: str, security_token: str, method: str,
                        url: str, body: bytes,
                        extra_headers: Optional[Dict[str, str]] = None,
                        now: Optional[float] = None) -> Dict[str, str]:
    """签名一个华为请求；返回需**原样合并到出站请求**的头映射。

    ⚠️ 返回的头一个都不能少：它们全部参与 canonical 计算并出现在
    SignedHeaders 里，漏发任何一个都会被服务端判为验签失败。

    - `extra_headers` 用于 `maas_type: benefit` 这类**必须参与签名**的头
      （`src/sign.ts:49-52` 注释：与 Rust 参考实现 sign_request_huawei 的
      extra_headers 一致）。
    - GET（排队状态轮询）不带请求体，因此**不加** `content-type`
      （`src/sign.ts:55-56`：带上它反而会因 body 为空而验签不一致）。
    - `uri` 不以 `/` 结尾时**补一个**（`src/sign.ts:38-39`）——这是华为签名的
      规范化要求，去掉会让签名与网关计算的不一致。
    """
    sp = urlsplit(url)
    uri = sp.path or "/"
    if not uri.endswith("/"):
        uri += "/"
    query = sp.query
    stamp = sdk_date_stamp(now)
    payload_hash = sha256_hex(body)

    headers: Dict[str, str] = {
        "host": sp.netloc,
        "x-sdk-date": stamp,
        "x-sdk-content-sha256": payload_hash,
        "x-security-token": security_token,
    }
    if extra_headers:
        for k, v in extra_headers.items():
            headers[k] = v
    if (method or "POST").upper() != "GET":
        headers["content-type"] = "application/json"

    canonical = build_canonical_request(method, uri, query, headers, payload_hash)
    string_to_sign = ("SDK-HMAC-SHA256\n"
                      f"{stamp}\n"
                      f"{sha256_hex(canonical.encode('utf-8'))}")
    signature = hmac_sha256_hex(sk.encode("utf-8"), string_to_sign.encode("utf-8"))
    signed_headers = ";".join(sorted(headers.keys()))
    headers["Authorization"] = (
        f"SDK-HMAC-SHA256 Access={ak},"
        f"SignedHeaders={signed_headers},Signature={signature}"
    )
    return headers


def signed_headers_for_request(ak: str, sk: str, security_token: str,
                               method: str, url: str, body: bytes = b"",
                               extra_signed_headers: Optional[Dict[str, str]] = None,
                               extra_unsigned_headers: Optional[Dict[str, str]] = None,
                               now: Optional[float] = None) -> Dict[str, str]:
    """出站请求头：签名头（去掉 host）+ 签名后追加的额外头。

    两处细节都照抄 Jet-Hub：
    - `host` **必须剔除**：由运行时按实际连接目标生成，手工设置会被 fetch/httpx
      拒绝或覆盖（`models.ts:94-97` 与 `codearts-credits.ts:344-347` 都这么做）。
    - `maas_type`（extra_signed_headers）**必须保留**：它已进 SignedHeaders，
      删掉就验签失败（`llm-adapter.ts:952-955` 的反面教训）。
    """
    signed = sign_request_huawei(ak, sk, security_token, method, url, body,
                                 extra_signed_headers, now=now)
    headers = {k: v for k, v in signed.items() if k != "host"}
    if extra_unsigned_headers:
        headers.update(extra_unsigned_headers)
    return headers


def is_benefit_model(model: str) -> bool:
    return str(model or "") in MAAS_TYPE_BENEFIT_MODELS


def maas_type_headers(model: str) -> Optional[Dict[str, str]]:
    """benefit 模型需要的**参与签名**的额外头；普通模型返回 None。"""
    return {"maas_type": "benefit"} if is_benefit_model(model) else None


def resolve_max_tokens(requested=None) -> int:
    """输出上限：显式传入优先，否则 65536（131072 会被后端拒成空流）。"""
    try:
        v = int(requested) if requested is not None else 0
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else DEFAULT_MAX_TOKENS


# ── PKCE / DPoP（浏览器 OAuth 用）───────────────────────────────

@dataclass
class PkcePair:
    code_verifier: str
    code_challenge: str


def generate_pkce_pair() -> PkcePair:
    """PKCE 配对：verifier = 48 字节 base64url，challenge = S256(verifier)。

    ⚠️ 注意这里的 challenge 是**标准 S256**（`src/oauth.ts:58-62` 就是这么算的）；
    URL 上的 `code_challenge_method` 才是不标准的 `SHA-256` 字面量。
    两者别混：portal 按 S256 校验 challenge，按字面量识别流程。
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return PkcePair(verifier, challenge)


def generate_dpop_private_jwk() -> dict:
    """生成 ES256（P-256）DPoP 私钥 JWK（仅私钥材料，公钥由 x/y 重建）。"""
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    pn = priv.private_numbers()
    return {
        "kty": "EC", "crv": "P-256",
        "x": _b64u(pn.public_numbers.x.to_bytes(32, "big")),
        "y": _b64u(pn.public_numbers.y.to_bytes(32, "big")),
        "d": _b64u(pn.private_value.to_bytes(32, "big")),
    }


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def public_jwk(jwk: dict) -> dict:
    """私钥 JWK → 公钥 JWK（只留 kty/crv/x/y）。"""
    return {"kty": jwk.get("kty", "EC"), "crv": jwk.get("crv", "P-256"),
            "x": jwk["x"], "y": jwk["y"]}


def dpop_public_jwk_from_private(jwk: dict) -> dict:
    """用 cryptography 重建公钥坐标（校验私钥自洽，也用于测试比对）。"""
    return public_jwk_from(private_key_from_jwk(jwk))


def generate_dpop_key_pair() -> dict:
    """DPoP 密钥对（JWK 形式）：{private_key_jwk, public_key_jwk}。"""
    priv = generate_dpop_private_jwk()
    return {"private_key_jwk": priv, "public_key_jwk": public_jwk(priv)}


def sign_dpop_jws(private_key_jwk: dict, htm: str, htu: str,
                  now: Optional[float] = None) -> str:
    """签发一枚 dpop+jwt JWS（htm=HTTP 方法，htu=**完整 URL**）。

    与仓库既有的 `dpop.sign_proof` 有两处差异，因此这里单独实现：
    1. 头里内嵌 `jwk`（公钥）—— Jet-Hub 的 CodeArts DPoP 明确带 jwk
       （`src/oauth.ts:82-84`），而 `sign_proof` 也带，但；
    2. CodeArts 的 payload **没有 `ath`**（不绑定 access token），
       且 `jti` 是 32 字节 hex（`src/oauth.ts:75-81`）。
    签名是 JWS raw r||s（64 字节），非 DER —— 与 `dpop.sign_proof` 同款。
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives import hashes

    priv = private_key_from_jwk(private_key_jwk)
    header = _b64u(json.dumps(
        {"alg": "ES256", "typ": "dpop+jwt", "jwk": public_jwk(private_key_jwk)},
        separators=(",", ":")).encode("utf-8"))
    payload = _b64u(json.dumps({
        "htm": htm,
        "htu": htu,
        "iat": int(now if now is not None else time.time()),
        "jti": secrets.token_hex(32),
    }, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{payload}".encode("utf-8")
    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"{header}.{payload}.{sig}"


# ── 授权 URL / 回调 ───────────────────────────────────────────

def build_oauth_login_url(port: int, pkce: PkcePair, ticket_id: str) -> str:
    """构造 portal 授权 URL（参数**逐个对齐** `buildOAuthLoginUrl`）。

    ⚠️ 三个不能动的点：
    1. `code_challenge_method=SHA-256`（非 S256，写错回退旧 ticket 流程）；
    2. **不要加 `auth_callback_url`** —— 真实插件 URL 里没有它，portal 仅凭
       `port` 参数构造回调；多余的参数会被视为异常并回退旧流程；
    3. `plugin-name` / `plugin-version` 是逆向常量，不能用本项目的版本号。

    端口先过 `ensure_callback_port`：低端口在此**立即失败**（而不是等用户登录完
    才发现 portal 悄悄走了旧流程 —— 那时候拿到的凭据没有 refresh_token）。
    """
    port = ensure_callback_port(port)
    return (f"{CODEARTS_PORTAL_AUTHORIZE_BASE}"
            f"?theme={CODEARTS_OAUTH_THEME}&locale={CODEARTS_OAUTH_LOCALE}"
            f"&uri_scheme={CODEARTS_CLIENT_ID}&client_id={CODEARTS_CLIENT_ID}"
            f"&port={int(port)}"
            f"&code_challenge={quote(pkce.code_challenge, safe='')}"
            f"&code_challenge_method={CODEARTS_CODE_CHALLENGE_METHOD}"
            f"&ticket_id={quote(ticket_id, safe='')}"
            f"&plugin-name={CODEARTS_LOGIN_PLUGIN_NAME}"
            f"&plugin-version={CODEARTS_LOGIN_PLUGIN_VERSION}")


def build_portal_login_result_url(succeeded: bool) -> str:
    """回调处理完 307 跳转到的 portal 结果页（对齐真实插件行为）。"""
    return (f"{CODEARTS_PORTAL_LOGIN_BASE}?login_succeed="
            f"{'true' if succeeded else 'false'}"
            f"&uri_scheme={CODEARTS_CLIENT_ID}&locale={CODEARTS_OAUTH_LOCALE}")


def redirect_uri_for_port(port: int) -> str:
    """换票请求里的 redirect_uri —— **必须**是 127.0.0.1 形态且与授权时同端口。

    portal 会校验 redirect_uri 与授权时下发的 port 一致；主机名也锁死
    127.0.0.1（写 localhost 或域名都会失败）。
    """
    return f"http://127.0.0.1:{ensure_callback_port(port)}{CODEARTS_REDIRECT_PATH}"


def pick_callback_port() -> int:
    """随机挑一个 ≥10000 的回调端口（对齐真实插件的重试逻辑）。

    AIGate 侧不监听该端口（远程部署时用户浏览器回不到服务器），
    端口值只用于拼授权 URL 与换票时的 redirect_uri —— 但**必须 ≥10000**，
    否则 portal 直接拒绝。
    """
    span = 65_536 - CODEARTS_MIN_CALLBACK_PORT
    return CODEARTS_MIN_CALLBACK_PORT + secrets.randbelow(span)


def ensure_callback_port(port) -> int:
    """校验回调端口 ≥10000，否则抛 ValueError（唯一会抛异常的纯函数）。

    为什么这里**必须**抛异常而不是静默换个端口：端口同时出现在授权 URL 与
    换票请求的 redirect_uri 里，两者必须完全一致。静默替换会让调用方拿到的
    redirect_uri 与实际发给用户的不符，换票必然失败，错误还会指向
    "invalid redirect_uri" 这种看不出根因的地方。

    ⚠️ 低端口（<10000）会被 portal 拒绝 —— 真实插件的做法是关闭监听、
    换随机 [10000, 65535] 端口重试（`src/login.ts:338-364`）。
    AIGate 不监听端口，故直接用 `pick_callback_port()` 生成。
    """
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"回调端口非法：{port!r}")
    if p < CODEARTS_MIN_CALLBACK_PORT:
        raise ValueError(
            f"回调端口必须 ≥{CODEARTS_MIN_CALLBACK_PORT}（当前 {p}）："
            f"portal 会拒绝低端口并静默回退旧 ticket 流程")
    if p > 65_535:
        raise ValueError(f"回调端口超出范围：{p}")
    return p


def generate_ticket_id() -> str:
    """portal 的 ticket_id（对齐 Jet-Hub：32 字节 hex）。"""
    return secrets.token_hex(32)


def parse_callback_payload(callback_url: str = "", code: str = "",
                           state: str = "", secret: str = "") -> dict:
    """解析回调：既接受整段 URL，也接受分离的 code/state/secret 参数。

    `kind` 取值：
    - `oauth`  —— 新流程收到 `code`（走 DPoP 换票，**有** refresh_token）；
    - `ticket` —— 旧流程回退收到 `secret`（只能换一次性 AK/SK，无 refresh_token）；
    - `none`   —— 两个都没有。

    解析失败永不抛异常（用户粘贴的可能是半截 URL）。
    """
    out = {"kind": "none", "code": str(code or ""), "state": str(state or ""),
           "secret": str(secret or "")}
    if callback_url:
        try:
            qs = _parse_qs(urlsplit(str(callback_url)).query)
        except Exception:
            qs = {}
        out["code"] = out["code"] or qs.get("code", "")
        out["state"] = out["state"] or qs.get("state", "")
        out["secret"] = out["secret"] or qs.get("secret", "")
    if out["code"]:
        out["kind"] = "oauth"
    elif out["secret"]:
        out["kind"] = "ticket"
    return out


def _parse_qs(query: str) -> Dict[str, str]:
    from urllib.parse import parse_qs
    return {k: (v[0] if v else "") for k, v in parse_qs(query or "").items()}


# ── token 交换 / 续期 ─────────────────────────────────────────

@dataclass
class CodeArtsCredential:
    """CodeArts 凭据（AK/SK/SecurityToken + 续期所需材料）。

    字段名与 Jet-Hub 的 `CodeArtsCredential`（src/types.ts:333-349）逐一对齐，
    便于日后与上游实现双向核对。
    """
    access_key_id: str = ""
    secret_access_key: str = ""
    security_token: str = ""
    expires_at: str = ""                      # ISO 字符串（stored 原样，不重算）
    domain_id: str = ""
    user_id: str = ""
    user_name: str = ""
    refresh_token: str = ""
    code_verifier: str = ""
    dpop_private_key_jwk: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
            "security_token": self.security_token,
            "expires_at": self.expires_at,
            "domain_id": self.domain_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "refresh_token": self.refresh_token,
            "code_verifier": self.code_verifier,
            "dpop_private_key_jwk": self.dpop_private_key_jwk,
        }

    @property
    def complete(self) -> bool:
        """签名请求需要三元组齐全（缺 SK 的凭据只能看不能发）。"""
        return bool(self.access_key_id and self.secret_access_key
                    and self.security_token)


def parse_token_response(data) -> CodeArtsCredential:
    """解析 `/v1/oauth2/tokens` 响应 → 凭据。

    字段名照抄 `src/oauth.ts:173-188`：ak/sk/st 在 `credentials` 下，
    `credentials.expiration` 是到期时间（ISO 字符串），refresh_token 在顶层。
    """
    if not isinstance(data, dict):
        return CodeArtsCredential()
    creds = data.get("credentials") if isinstance(data.get("credentials"), dict) else {}
    expires = str(creds.get("expiration") or "")
    return CodeArtsCredential(
        access_key_id=str(creds.get("access_key_id") or ""),
        secret_access_key=str(creds.get("secret_access_key") or ""),
        security_token=str(creds.get("security_token") or ""),
        expires_at=normalize_expires_at(expires),
        refresh_token=str(data.get("refresh_token") or ""),
    )


def normalize_expires_at(value) -> str:
    """到期时间归一化为 ISO 字符串（本地时间戳也接受）。

    华为下发的是 `2026-08-25T12:34:56Z` 形态；若下游实现改成时间戳，
    这里统一成 ISO 让 `expires_in` 计算与 DB 存储只有一套口径。
    """
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    s = str(value).strip()
    # 已含时区/Z 的原样保留（只有格式信息缺失时才补）
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def expires_in_from(expires_at: str, fallback: int = 86_400) -> int:
    """`expires_at` → 剩余秒数（无法解析时回退 fallback，与 Jet-Hub 的 +24h 一致）。

    故意不返回 0：上游偶尔不下发 expiration，返回 0 会让刷新调度每轮都触发，
    而 CodeArts 的 refresh_token 一次性 —— 一次多余刷新就废掉凭据。
    """
    from datetime import timedelta
    s = str(expires_at or "").strip()
    if not s:
        return int(fallback)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(60, int((dt - datetime.now(timezone.utc)).total_seconds()))
    except ValueError:
        return int(fallback)


def is_refresh_token_reused_error(text: str) -> bool:
    """判定「refresh_token 已被用过」（一次性轮换的直接症状）。

    实测文案：`STS5.1806 the refresh token has been used`
    （`tests/e2e/codearts-credential.ts:9-13`）。识别出它意味着**并发刷新**已经把
    这条凭据作废了 —— 只能重新登录，继续重试毫无意义。
    """
    t = str(text or "").lower()
    return ("sts5.1806" in t
            or "refresh token has been used" in t
            or "refreshtokenhasbeenused" in t)


def is_refresh_token_dead_error(text: str) -> bool:
    """判定 refresh_token 终态失效（应停止续期、提示重新登录）。

    对齐 `src/oauth.ts:127-135`：`invalid_grant`、`ExpiredRefreshToken`、
    `InvalidDPoPHeader` 三种都算终态 —— Jet-Hub 明确说明后者也算，
    否则 error_code 为 InvalidDPoPHeader 时会每 10 分钟无限重试。
    """
    t = str(text or "")
    low = t.lower()
    return ("invalid_grant" in low
            or "expiredrefreshtoken" in low
            or "invaliddpopheader" in low
            or "sts5.1806" in low
            or "refresh token has been used" in low)


async def request_token(body: Dict[str, str], dpop_private_jwk: dict,
                        now: Optional[float] = None,
                        ) -> Tuple[Optional[dict], str, bool]:
    """向 STS 端点发一次带 DPoP 的 token 请求 → (data, error, dead)。

    - body 是 **form-urlencoded**（`src/oauth.ts:104-111`），不是 JSON；
    - DPoP 头是 ES256 JWS，`htu` 为**完整端点 URL**；
    - `dead=True` 表示 refresh_token 终态失效（调用方应停止调度、提示重登）。
    """
    if not isinstance(dpop_private_jwk, dict) or not dpop_private_jwk.get("d"):
        return None, "缺少 DPoP 私钥（DPoP 头是换票的硬要求）", False
    try:
        dpop = sign_dpop_jws(dpop_private_jwk, "POST", CODEARTS_STS_TOKEN_ENDPOINT,
                             now=now)
    except Exception as e:
        return None, f"DPoP 签名失败：{type(e).__name__}: {e}", False
    headers = {"DPoP": dpop,
               "Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=CODEARTS_TOKEN_TIMEOUT) as client:
            r = await client.post(CODEARTS_STS_TOKEN_ENDPOINT, headers=headers,
                                  content=_form_encode(body))
    except Exception as e:
        return None, f"token 请求网络失败：{type(e).__name__}", False
    text = ""
    try:
        text = r.text
    except Exception:
        text = ""
    data = None
    try:
        data = r.json()
    except Exception:
        data = None
    if r.status_code >= 400 or not isinstance(data, dict) or not data.get("credentials"):
        detail = text[:300] if text else ""
        if isinstance(data, dict):
            detail = json.dumps(data, ensure_ascii=False)[:300]
        return None, f"token 请求失败：HTTP {r.status_code} {detail}".strip(), \
            is_refresh_token_dead_error(detail)
    return data, "", False


def _form_encode(body: Dict[str, str]) -> str:
    from urllib.parse import urlencode
    return urlencode({k: v for k, v in (body or {}).items()}, quote_via=quote)


async def exchange_authorization_code(code: str, code_verifier: str, port: int,
                                      dpop_private_jwk: dict,
                                      ) -> Tuple[Optional[CodeArtsCredential], str]:
    """授权码换票 → (credential, error)。

    body 四字段照抄 `src/oauth.ts:148-154`：
    `client_id / code / code_verifier / grant_type / redirect_uri`。
    redirect_uri 用**回调时的同一个 port**（portal 会校验一致性）。
    """
    body = {
        "client_id": CODEARTS_CLIENT_ID,
        "code": str(code or ""),
        "code_verifier": str(code_verifier or ""),
        "grant_type": GRANT_AUTHORIZATION_CODE,
        "redirect_uri": redirect_uri_for_port(port),
    }
    data, err, _dead = await request_token(body, dpop_private_jwk)
    if data is None:
        return None, err
    cred = parse_token_response(data)
    if not cred.complete:
        return None, "token 响应缺少 AK/SK/SecurityToken 三元组"
    cred.code_verifier = str(code_verifier or "")
    cred.dpop_private_key_jwk = dpop_private_jwk
    return cred, ""


async def refresh_token(refresh_plain: str, code_verifier: str,
                        dpop_private_jwk: dict,
                        ) -> Tuple[Optional[CodeArtsCredential], str, bool]:
    """续期 → (new_credential, error, dead)。

    ⚠️⚠️ **调用方必须串行化本函数**（同一 (provider, owner) 同时只允许一次在途
    刷新）。CodeArts 的 refresh_token **一次性轮换**：用一次即作废，服务端回
    `STS5.1806 the refresh token has been used`。两个并发刷新会互相作废，
    结果是**只能让用户重新走浏览器登录**（Jet-Hub 的 e2e 注释记录了这个坑，
    并在开发中真实踩过一次）。

    返回值的 `new_credential.refresh_token` 是**新的** refresh_token，
    调用方必须**立即回写持久化**；回写失败等于把凭据永久废掉。
    故意不在这里吞掉轮换语义（例如缓存旧值）—— 那只会让状态更难推理。

    `dead=True`（`is_refresh_token_dead_error`）表示终态失效：停止调度 + 提示重登。
    """
    rt = str(refresh_plain or "")
    if not rt:
        return None, "no refresh_token stored（旧 ticket 凭据不可静默续期，请重新登录）", True
    body = {
        "client_id": CODEARTS_CLIENT_ID,
        "code_verifier": str(code_verifier or ""),
        "grant_type": GRANT_REFRESH_TOKEN,
        "refresh_token": rt,
    }
    data, err, dead = await request_token(body, dpop_private_jwk)
    if data is None:
        if is_refresh_token_reused_error(err):
            # 单调、可读的说明：这条路径几乎总是并发刷新造成的
            return None, ("refresh_token 已被使用过（一次性轮换）："
                          "可能发生了并发续期，请重新登录"), True
        return None, err, dead
    cred = parse_token_response(data)
    if not cred.complete:
        return None, "refresh 响应缺少 AK/SK/SecurityToken 三元组", True
    cred.code_verifier = str(code_verifier or "")
    cred.dpop_private_key_jwk = dpop_private_jwk
    return cred, "", False


async def exchange_ticket(ticket_id: str, secret: str,
                          max_attempts: int = 120,
                          ) -> Tuple[Optional[CodeArtsCredential], str]:
    """旧 ticket 流程回退：轮询 `/snap-manager/v1/login/ticket` 取 AK/SK。

    `src/login.ts:77-113` 的行为：每次 1 秒间隔、瞬时失败**跳过继续**
    （不是致命错误）、上限 120 次。返回的凭据**没有 refresh_token**
    —— 到期只能重新登录，调用方应在凭据里如实体现（不要伪造一个空刷新路径）。
    """
    url = (f"{CODEARTS_TICKET_ENDPOINT}?ticket_id={quote(str(ticket_id), safe='')}"
           f"&secret={quote(str(secret), safe='')}")
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "plugin-name": CODEARTS_LOGIN_PLUGIN_NAME,
        "plugin-version": CODEARTS_LOGIN_PLUGIN_VERSION,
    }
    import asyncio
    last = "凭据未就绪"
    for i in range(max(1, int(max_attempts))):
        if i > 0:
            await asyncio.sleep(1.0)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url, headers=headers)
        except Exception as e:
            last = f"网络失败：{type(e).__name__}"
            continue
        if r.status_code >= 400:
            last = f"HTTP {r.status_code}"
            continue
        try:
            data = r.json()
        except Exception:
            last = "响应不是 JSON"
            continue
        cred = parse_ticket_response(data)
        if cred is not None:
            return cred, ""
    return None, f"ticket 流程超时（{max_attempts} 次轮询）：{last}"


def parse_ticket_response(data) -> Optional[CodeArtsCredential]:
    """解析 ticket 响应（两种形态：`credential` 内嵌 / `result` 平铺）。

    照抄 `src/login.ts:33-62`，字段名别名一个都不能少：
    `securitytoken` 与 `securityToken` 都出现过，`access` 也可能叫 `accessKeyId`。
    """
    if not isinstance(data, dict):
        return None
    cred = data.get("credential")
    if isinstance(cred, dict):
        access = str(cred.get("access") or "")
        st = str(cred.get("securitytoken") or cred.get("securityToken") or "")
        if access and st:
            return CodeArtsCredential(
                access_key_id=access,
                secret_access_key=str(cred.get("secret") or ""),
                security_token=st,
                expires_at=normalize_expires_at(
                    cred.get("expires_at") or cred.get("expiresAt")),
                domain_id=str(data.get("domain_id") or ""),
                user_id=str(data.get("user_id") or ""),
                user_name=str(data.get("user_name") or ""),
            )
    result = data.get("result")
    if isinstance(result, dict):
        ak = str(result.get("accessKeyId") or "")
        st = str(result.get("securityToken") or "")
        if ak and st:
            return CodeArtsCredential(
                access_key_id=ak,
                secret_access_key=str(result.get("secretAccessKey") or ""),
                security_token=st,
                expires_at=normalize_expires_at(
                    result.get("expiration") or result.get("expiresAt")),
            )
    return None


# ── 聊天请求体（CodeArts 方言）────────────────────────────────

def content_to_text(content) -> str:
    """把消息内容载荷压平成纯文本。

    CodeArts 端点**拒绝非 text 块**（图片等）并返回空流
    （`src/llm-adapter.ts:76-84`），因此只保留 `type=='text'` 的块。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return "" if content is None else str(content)


def normalize_tool_arguments(raw) -> str:
    """tool_call arguments 归一化为合法 JSON 字符串。

    空串 / 非法 JSON / 非对象都回退 `{}`（`src/sse.ts:125-138`）：
    后端在无参数工具只发一个空分片时给的就是空串，原样回传会被判参数非法，
    把整轮对话卡死。
    """
    s = raw if isinstance(raw, str) else json.dumps(raw or {}, ensure_ascii=False)
    trimmed = s.strip()
    if not trimmed:
        return "{}"
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        return "{}"
    if parsed is None or not isinstance(parsed, dict):
        return "{}"
    return trimmed


def resolve_tool_pairing(messages: List[dict]) -> Tuple[set, set]:
    """剔除无法配对的 tool_call / tool_result（`src/sse.ts:91-123`）。

    为什么必须做：带 `tool_calls` 的 assistant 消息，其每个 id 都必须紧跟一条
    对应的 `role:'tool'` 结果；反之亦然。工具执行失败时上游客户端只持久化了
    tool_calls、写不回结果，这条坏历史会被**每次请求原样重放**，于是后端对
    之后每条消息都返回 400 —— 表现为「任务突然中断，此后发什么都没回复」。

    一批 tool_calls 只有**全部**拿到结果才保留：部分保留照样会留下无结果的调用。
    """
    all_result_ids = set()
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool-result":
                    all_result_ids.add(str(b.get("toolCallId")))
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            all_result_ids.add(str(msg["tool_call_id"]))
    keep_call_ids = set()
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        ids = [str((c or {}).get("id")) for c in calls if isinstance(c, dict)]
        if ids and all(i in all_result_ids for i in ids):
            keep_call_ids.update(ids)
    keep_result_ids = {i for i in keep_call_ids if i in all_result_ids}
    return keep_call_ids, keep_result_ids


def serialize_messages(messages: List[dict], system: str = "",
                       dsml_system_prompt: str = "") -> List[dict]:
    """把 OpenAI 格式消息序列化为 CodeArts 传输格式（`src/llm-adapter.ts:95-154`）。

    三条硬约束：
    1. **每条 assistant 消息都必须带 `reasoning_content`**（无推理时为**空串**，
       字段必须在）—— deepseek-v4 系缺该字段直接 400；
    2. 工具结果展开为独立的 `role:'tool'` 消息（搭载在 user 消息里的
       `tool-result` 块要拆出来），空输出补 `(no output)`；
    3. 纯文本 user 消息**原样透传** —— 若用「块数组」的取值路径，字符串 content
       会被序列化成空串，模型看不到任务指令。

    `system` 为顶层 system 提示（harness 的独立 persona/标题生成 prompt）：
    后端只认 messages 里的 system 角色，必须显式插入，否则模型看不到它。
    `dsml_system_prompt` 是 DSML 工具说明，插在 system **之后、user 之前**
    （`src/llm-adapter.ts:886-893` 的实证：push 到末尾会让模型把思考写进正文）。
    """
    keep_calls, keep_results = resolve_tool_pairing(messages)
    wire: List[dict] = []

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role == "assistant":
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            calls = []
            # tool_calls 有两条来源：标准 OpenAI 字段，以及 harness 的
            # [{type:'tool-call'}] 块数组。两者都要支持（Jet-Hub 两种都处理）。
            raw_calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
            for c in raw_calls:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id") or "")
                if cid and cid not in keep_calls:
                    continue
                fn = c.get("function") if isinstance(c.get("function"), dict) else {}
                calls.append({
                    "id": cid,
                    "type": "function",
                    "function": {"name": str(fn.get("name") or ""),
                                 "arguments": normalize_tool_arguments(
                                     fn.get("arguments") or "")},
                })
            for b in blocks:
                if not isinstance(b, dict) or b.get("type") != "tool-call":
                    continue
                cid = str(b.get("id") or "")
                if cid and cid not in keep_calls:
                    continue
                calls.append({
                    "id": cid, "type": "function",
                    "function": {
                        "name": str(b.get("name") or ""),
                        "arguments": normalize_tool_arguments(b.get("arguments") or ""),
                    },
                })
            reasoning = msg.get("reasoning_content")
            if not isinstance(reasoning, str):
                # harness 把推理放在 [{type:'reasoning'}] 块里
                reasoning = "".join(
                    str(b.get("text") or "") for b in blocks
                    if isinstance(b, dict) and b.get("type") == "reasoning")
            entry = {
                "role": "assistant",
                "content": content_to_text(content),
                # ⚠️ 字段恒在（无推理时空串）——缺失即 400
                "reasoning_content": reasoning,
            }
            if calls:
                entry["tool_calls"] = calls
            wire.append(entry)
            continue
        if role == "system":
            wire.append({"role": "system", "content": content_to_text(msg.get("content"))})
            continue
        if role == "tool":
            tcid = str(msg.get("tool_call_id") or "")
            if tcid and tcid not in keep_results:
                continue
            entry = {"role": "tool", "tool_call_id": tcid,
                     "content": content_to_text(msg.get("content")) or "(no output)"}
            wire.append(entry)
            continue
        # user（含搭载的 tool-result 块）
        content = msg.get("content")
        results = []
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool-result":
                    results.append(b)
        text = content_to_text(content)
        if text or not results:
            wire.append({"role": "user", "content": text})
        for b in results:
            tcid = str(b.get("toolCallId") or "")
            if tcid and tcid not in keep_results:
                continue
            wire.append({"role": "tool", "tool_call_id": tcid,
                         "content": content_to_text(b.get("content")) or "(no output)"})
    # 顶层 system + DSML 说明的插入位置（DSML 必须在 system 之后、user 之前）
    if system:
        wire.insert(0, {"role": "system", "content": system})
    if dsml_system_prompt:
        idx = 0
        for i, m in enumerate(wire):
            if m.get("role") == "system":
                idx = i + 1
        wire.insert(idx, {"role": "system", "content": dsml_system_prompt})
    return wire


def is_deepseek_v4_model(model: str) -> bool:
    """deepseek-v4 系（flash/pro）—— 必须走 DSML，否则大参数写入必断流。"""
    import re
    return bool(re.match(r"^deepseek-v4-(flash|pro)$", str(model or "")))


def needs_dsml_tool_mode(model: str) -> bool:
    """是否应采用 DSML 工具调用模式（当前等价于 is_deepseek_v4_model）。

    非 deepseek-v4 模型（openpangu / GLM-5.2 等）走华为标准 IAM AK/SK 鉴权，
    不经 CodeArts Agent APIG 网关，无 60s 空闲断连问题，保持标准 tool_calls
    （`src/llm-adapter.ts:196-202`）。
    """
    return is_deepseek_v4_model(model)


def build_dsml_system_prompt(tools: List[dict]) -> str:
    """把 OpenAI function schema 注入 system 提示，让模型用 DSML 语法调工具。

    适配器**不发 `tools` 字段**（发了模型就走标准 tool_calls 一次性打包路径，
    大参数生成期间 SSE 静默 >60s，被 APIG 网关掐断），改用本提示 + 解析
    `delta.content` 里的 DSML 块。
    """
    payload = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        payload.append({
            "name": fn.get("name"),
            "description": fn.get("description"),
            "parameters": fn.get("parameters"),
        })
    tool_json = json.dumps(payload, ensure_ascii=False, indent=2)
    return "\n".join([
        "以下是你可用的工具及其 JSON Schema。当需要调用工具完成任务时，",
        "必须使用原生 DSML 工具调用语法输出，格式如下：",
        DSML_TOOL_CALLS_OPEN + DSML_INVOKE_OPEN + ' name="工具名">'
        + DSML_PARAM_OPEN + ' name="参数名" string="true">参数值'
        + DSML_PARAM_CLOSE + DSML_INVOKE_CLOSE + DSML_TOOL_CALLS_CLOSE,
        "",
        "规则：",
        "- 工具名必须是下面列表中的 name。",
        "- 每个参数用一个 " + DSML_PARAM_OPEN + " 标签包裹，参数值放在标签之间。",
        '- 字符串参数加 string="true" 属性；对象/数组/数字/布尔参数不要加该属性。',
        "- 一次可以输出多个 " + DSML_INVOKE_OPEN + " 调用（工具可以并行）。",
        "- 文件内容请一次性完整写入单个 write 调用的 content 参数，不要拆分或省略。",
        "",
        "工具列表（JSON Schema）：",
        tool_json,
    ])


def build_chat_body(request, session_id: str = "", dsml: bool = False,
                    tools: Optional[List[dict]] = None) -> dict:
    """构造 chat/completions 请求体（字段逐条对齐 `llm-adapter.ts:895-920`）。

    - `prompt_cache_key` 缺失时服务端缓存命中恒为 0（实测 2026-08-24）；
    - `max_tokens` 默认 65536（65536 可用、131072 会被后端拒成空流）；
    - `stream` **恒为 true**（本模块只做流式，非流式由适配器聚合）；
    - DSML 模式下**不带 `tools` 字段**，改用 system 注入。
    """
    body_msgs = []
    tools_list = [t for t in (tools or []) if isinstance(t, dict)]
    if dsml and tools_list:
        dsml_prompt = build_dsml_system_prompt(tools_list)
    else:
        dsml_prompt = ""
    system = ""
    try:
        raw_msgs = request.messages or []
    except AttributeError:
        raw_msgs = []
    for m in raw_msgs:
        d = m.model_dump(exclude_none=True) if hasattr(m, "model_dump") else dict(m or {})
        body_msgs.append(d)
    wire = serialize_messages(body_msgs, system=system, dsml_system_prompt=dsml_prompt)
    body: dict = {
        "model": str(getattr(request, "model", "") or ""),
        "messages": wire,
        "stream": True,
        "prompt_cache_key": session_id or "",
        "include": ["reasoning.encrypted_content"],
        "reasoning_summary": "auto",
        "tool_stream": True,
        "max_tokens": resolve_max_tokens(getattr(request, "max_tokens", None)),
    }
    # DSML 模式不发 tools；否则原样透传（空列表也不发）
    if tools_list and not dsml:
        body["tools"] = tools_list
    for field in ("temperature", "top_p", "stop", "seed", "presence_penalty",
                  "frequency_penalty"):
        v = getattr(request, field, None)
        if v is not None:
            body[field] = v
    return body


# ── DSML 流式解析（三态状态机，照抄 llm-adapter.ts:620-725）──────────

def parse_dsml_invoke(block: str) -> Optional[dict]:
    """解析单个 DSML invoke 块 → {name, arguments(JSON 字符串)}。"""
    import re
    m = re.search(r'name\s*=\s*"([^"]*)"', block)
    if not m:
        return None
    name = m.group(1)
    params: Dict[str, object] = {}
    cursor = 0
    while True:
        open_start = block.find(DSML_PARAM_OPEN, cursor)
        if open_start == -1:
            break
        open_end = block.find(">", open_start)
        if open_end == -1:
            break
        open_tag = block[open_start:open_end + 1]
        pm = re.search(r'name\s*=\s*"([^"]*)"', open_tag)
        if not pm:
            cursor = open_end + 1
            continue
        pname = pm.group(1)
        close_start = block.find(DSML_PARAM_CLOSE, open_end + 1)
        if close_start == -1:
            break
        value = block[open_end + 1:close_start]
        if re.search(r'string\s*=\s*"true"', open_tag):
            params[pname] = try_parse_scalar(value)
        else:
            try:
                parsed = json.loads(value)
                params[pname] = (try_parse_scalar(parsed)
                                 if isinstance(parsed, str) else parsed)
            except ValueError:
                params[pname] = value
        cursor = close_start + len(DSML_PARAM_CLOSE)
    return {"name": name, "arguments": json.dumps(params, ensure_ascii=False)}


def try_parse_scalar(value: str):
    """把标量字符串还原成原始类型（模型常把数字/布尔误标 string="true"）。"""
    if value == "":
        return ""
    s = value.strip()
    if s == "":
        return value
    if s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        try:
            decoded = json.loads(s)
            if isinstance(decoded, str):
                return try_parse_scalar(decoded)
            return decoded
        except ValueError:
            pass
    import re
    if re.match(r"^-?\d+$", s):
        return int(s)
    if re.match(r"^-?\d+\.\d+$", s):
        return float(s)
    if s.startswith("[") or s.startswith("{"):
        try:
            return json.loads(s)
        except ValueError:
            pass
    return value


def parse_dsml_tool_calls(block: str) -> Optional[List[dict]]:
    """从一段已闭合的 tool_calls 块解析全部 invoke；失败返回 None（调用方回退纯文本）。"""
    inner = block
    if inner.startswith(DSML_TOOL_CALLS_OPEN):
        inner = inner[len(DSML_TOOL_CALLS_OPEN):]
    if inner.endswith(DSML_TOOL_CALLS_CLOSE):
        inner = inner[:-len(DSML_TOOL_CALLS_CLOSE)]
    calls: List[dict] = []
    cursor = 0
    while True:
        open_start = inner.find(DSML_INVOKE_OPEN, cursor)
        if open_start == -1:
            break
        open_end = inner.find(">", open_start)
        if open_end == -1:
            break
        close_start = inner.find(DSML_INVOKE_CLOSE, open_end + 1)
        if close_start == -1:
            break
        invoke_block = inner[open_start:close_start + len(DSML_INVOKE_CLOSE)]
        parsed = parse_dsml_invoke(invoke_block)
        if parsed is None:
            return None
        calls.append(parsed)
        cursor = close_start + len(DSML_INVOKE_CLOSE)
    return calls


def longest_open_prefix_tail(buffer: str, prefixes: List[str]) -> int:
    """缓冲区末尾与任一开标签的最长公共前缀长度（决定保留多少等下次 feed）。"""
    keep = 0
    max_check = min(len(buffer), max((len(p) for p in prefixes), default=0))
    for i in range(1, max_check + 1):
        tail = buffer[len(buffer) - i:]
        if any(p.startswith(tail) for p in prefixes):
            keep = i
    return keep


class DsmlContentExtractor:
    """流式 DSML 提取器（三态：normal / in-thought / in-dsml）。

    - `<thought>` 内容作为 reasoning 增量输出（显示在思考区，不进正文）；
    - `<｜DSML｜tool_calls>` 完整块解析为结构化 tool-call；
    - 其余文本立即 flush（不做过度缓冲，避免短文本延迟输出）；
    - 截断的残留（流结束时）按状态原样放行，**绝不吞掉用户可见内容**：
      不完整的 DSML 块要补回开标签（否则 UI 会看到缺头残片）；
      不完整的 thought 块作为 reasoning 放行（避免推理泄漏到正文）。

    注意 `content` 与 `reasoning_content` 两个通道**各用一个实例** ——
    共用会导致一个通道的状态机吞掉另一个通道的 DSML 块
    （`src/llm-adapter.ts:1092-1101` 记录的实测缺陷）。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._state = "normal"

    def feed(self, chunk: str) -> Dict[str, object]:
        text = ""
        reasoning = ""
        tool_calls: List[dict] = []
        self._buffer += chunk or ""
        while True:
            if self._state == "normal":
                thought_idx = self._buffer.find(THOUGHT_OPEN)
                dsml_idx = self._buffer.find(DSML_TOOL_CALLS_OPEN)
                open_idx = -1
                next_state = "in-thought"
                if thought_idx != -1 and (dsml_idx == -1 or thought_idx < dsml_idx):
                    open_idx, next_state = thought_idx, "in-thought"
                elif dsml_idx != -1:
                    open_idx, next_state = dsml_idx, "in-dsml"
                if open_idx == -1:
                    keep = longest_open_prefix_tail(
                        self._buffer, [THOUGHT_OPEN, DSML_TOOL_CALLS_OPEN])
                    if keep == 0:
                        text += self._buffer
                        self._buffer = ""
                    elif len(self._buffer) > keep:
                        text += self._buffer[:len(self._buffer) - keep]
                        self._buffer = self._buffer[len(self._buffer) - keep:]
                    break
                if open_idx > 0:
                    text += self._buffer[:open_idx]
                self._buffer = self._buffer[open_idx:]
                open_len = (len(THOUGHT_OPEN) if next_state == "in-thought"
                            else len(DSML_TOOL_CALLS_OPEN))
                self._buffer = self._buffer[open_len:]
                self._state = next_state
                continue
            if self._state == "in-thought":
                close_idx = self._buffer.find(THOUGHT_CLOSE)
                if close_idx == -1:
                    keep = longest_open_prefix_tail(self._buffer, [THOUGHT_CLOSE])
                    if keep == 0:
                        reasoning += self._buffer
                        self._buffer = ""
                    elif len(self._buffer) > keep:
                        reasoning += self._buffer[:len(self._buffer) - keep]
                        self._buffer = self._buffer[len(self._buffer) - keep:]
                    break
                if close_idx > 0:
                    reasoning += self._buffer[:close_idx]
                self._buffer = self._buffer[close_idx + len(THOUGHT_CLOSE):]
                self._state = "normal"
                continue
            # in-dsml
            close_idx = self._buffer.find(DSML_TOOL_CALLS_CLOSE)
            if close_idx == -1:
                break
            block = self._buffer[:close_idx + len(DSML_TOOL_CALLS_CLOSE)]
            parsed = parse_dsml_tool_calls(block)
            if parsed is None:
                text += block
            else:
                tool_calls.extend(parsed)
            self._buffer = self._buffer[close_idx + len(DSML_TOOL_CALLS_CLOSE):]
            self._state = "normal"
            continue
        return {"text": text, "reasoning": reasoning, "tool_calls": tool_calls}

    def flush(self) -> Dict[str, str]:
        remaining = self._buffer
        self._buffer = ""
        if self._state == "in-thought":
            self._state = "normal"
            return {"text": "", "reasoning": remaining}
        if self._state == "in-dsml":
            self._state = "normal"
            return {"text": DSML_TOOL_CALLS_OPEN + remaining, "reasoning": ""}
        self._state = "normal"
        return {"text": remaining, "reasoning": ""}


# ── 排队（并发超限轮询）─────────────────────────────────────

def queue_status_url(model: str, task_id: str) -> str:
    return (f"{CODEARTS_SNAP_ACCESS_BASE}{CODEARTS_QUEUE_STATUS_PATH}"
            f"?model={quote(str(model or ''), safe='')}"
            f"&task_id={quote(str(task_id or ''), safe='')}")


def is_queue_error(status_code: int, body: str) -> bool:
    """HTTP 400 且命中并发超限文案（`src/llm-adapter.ts:326-331`）。

    主判据是业务码 `TM.00001041`；其余的 peak hours / high demand 是**文案兜底**
    （openpangu 等模型的错误码/状态码与 GLM 不同，但仍会进队列）。
    """
    if int(status_code or 0) != 400:
        return False
    import re
    b = str(body or "")
    return ("TM.00001041" in b
            or bool(re.search(r"peak\s+usage|try\s+again\s+after|peak\s+hours", b, re.I))
            or bool(re.search(r"high\s+demand|too\s+many\s+requests", b, re.I)))


def is_sse_queue_error_code(code: str) -> bool:
    """SSE 流内嵌的排队/限流错误码（HTTP 200 但 error_code 是限流）。

    实测 `InferHub.ModelArts.81111.429`（TPM 每分钟 token 超限）。不识别它就会
    把流当正常结束，表现为「思考后无输出」（`src/llm-adapter.ts:347-357`）。
    """
    import re
    c = str(code or "")
    if c == "TM.00001041":
        return True
    return bool(re.search(r"81111|TPM|429|rate.?limit|too many requests|排队|限流", c, re.I))


def parse_queue_status(body) -> Optional[dict]:
    """解析排队状态；无法识别返回 None（调用方按原错误分类抛出）。

    `status` 只认四个取值：waiting / working / error / queue_full。
    `queue_position` 缺失给 -1（Jet-Hub 同款：用哨兵值区分「未知」与 0）。
    """
    if not isinstance(body, dict):
        return None
    status = body.get("status")
    if status not in ("waiting", "working", "error", "queue_full"):
        return None
    pos = body.get("queue_position")
    try:
        pos = int(pos) if pos is not None else -1
    except (TypeError, ValueError):
        pos = -1
    return {"status": status, "queue_position": pos,
            "message": str(body.get("message") or "")}


def is_auth_error(status_code: int, body: str) -> bool:
    """鉴权失败（凭据过期/被吊销）→ 可静默刷新一次后重试。

    CodeArts 经华为 APIG 网关鉴权：SecurityToken 过期时返回 `APIG.0602`
    （"Invalid token"），HTTP 通常是 401，也观察到 403
    （`src/llm-adapter.ts:342-345`）。入口按 expires_at 预判无法覆盖
    「后端提前吊销 / 时钟偏差」这两种情况，故需要这层兜底。
    """
    import re
    if int(status_code or 0) in (401, 403):
        return True
    b = str(body or "")
    return ("APIG.0602" in b
            or bool(re.search(r"invalid\s+token|token\s+expired|token\s+is\s+invalid", b, re.I)))


def http_error_code(status_code: int, body: str) -> str:
    """HTTP 错误 → 与仓库其它适配器一致的错误码词汇。"""
    import re
    if int(status_code or 0) in (401, 403):
        return "AUTH"
    if int(status_code or 0) == 429:
        return "RATE_LIMIT"
    if int(status_code or 0) == 400:
        if re.search(r"context|too long|exceed|maximum.*token", str(body or ""), re.I):
            return "CONTEXT_WINDOW_EXCEEDED"
        return "INVALID_REQUEST"
    if int(status_code or 0) >= 500:
        return "SERVER"
    return f"HTTP_{status_code}"


def is_transport_error(exc: BaseException) -> bool:
    """SSE 传输级故障（连接被对端掐断 / socket 重置）→ 可安全重试整个请求。

    实测：模型生成超长内容时两次 chunk 之间静默数十秒，APIG 网关 **~60s 空闲
    必断连**（`src/llm-adapter.ts:284-301`）。这类错误归为 TRANSPORT 而非
    UNKNOWN，让上层重试该步骤而不是直接失败。
    """
    if not isinstance(exc, BaseException):
        return False
    msg = str(exc).lower()
    name = type(exc).__name__
    if "terminated" in msg or "incomplete" in msg:
        return True
    if name.startswith("UND_ERR_"):
        return True
    for marker in ("econnreset", "epipe", "socket hang up", "connection reset",
                   "server disconnected", "remoteprotocolerror", "readtimeout",
                   "connecttimeout", "networkerror"):
        if marker in msg or marker in name.lower():
            return True
    return False


# ── 模型列表 ──────────────────────────────────────────────

def normalize_model_id(model_id: str) -> str:
    """去掉 id 末尾的 `-NNNN` 日期后缀（`src/models.ts:41-49`）。

    远端目录下发 `deepseek-v4-flash-0731`，但 chat 端点只认
    `deepseek-v4-flash`（带上后缀报 `InferHub.002002009.404`
    "The model is not registered"）。

    只匹配**恰好 4 位数字**的后缀：`glm-5.3-flash` 这类无后缀 id 不会被误改。
    """
    s = str(model_id or "")
    if len(s) > 5:
        suffix = s[-5:]
        if suffix.startswith("-") and suffix[1:].isdigit():
            return s[:-5]
    return s


def is_vl_model(model_id: str) -> bool:
    """视觉（VL）多模态模型：上下文小、不支持工具调用 → 从列表隐藏。

    只通过 analyzeImage 类工具间接调用（`src/models.ts:51-58`）。
    """
    s = str(model_id or "").lower()
    return "-vl-" in s or s.endswith("-vl")


def parse_model_entry(item, seen: set) -> Optional[dict]:
    """解析目录里的单个模型条目（去重 + 过滤 VL + 去日期后缀）。"""
    if not isinstance(item, dict):
        return None
    raw_id = item.get("model_id")
    if not isinstance(raw_id, str) or not raw_id:
        return None
    mid = normalize_model_id(raw_id)
    if is_vl_model(mid):
        return None
    raw_name = item.get("model_name")
    name = normalize_model_id(raw_name) if isinstance(raw_name, str) and raw_name else mid
    if mid in seen:
        return None
    seen.add(mid)
    return {"id": mid, "name": name}


def parse_gateway_models(body) -> List[dict]:
    """解析 opengw `gateway/config` → `result.models`（benefit 模型）。"""
    if not isinstance(body, dict):
        return []
    result = body.get("result")
    if not isinstance(result, dict):
        return []
    arr = result.get("models")
    if not isinstance(arr, list):
        return []
    seen: set = set()
    out = []
    for item in arr:
        entry = parse_model_entry(item, seen)
        if entry:
            out.append(entry)
    return out


def parse_builtin_models(body) -> List[dict]:
    """解析 snap-access `/v1/model/builtin` → 顶层 `builtinModels`。"""
    if not isinstance(body, dict):
        return []
    arr = body.get("builtinModels")
    if not isinstance(arr, list):
        return []
    seen: set = set()
    out = []
    for item in arr:
        entry = parse_model_entry(item, seen)
        if entry:
            out.append(entry)
    return out


def merge_models(*groups) -> List[dict]:
    """按传入顺序合并多个模型分组并**全局去重**（先到先得）。

    顺序即优先级：gateway（benefit 模型）在前，builtin 在后
    （`src/models.ts:120-165`）。
    """
    out: List[dict] = []
    seen: set = set()
    for group in groups:
        for m in group or []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            out.append({"id": mid, "name": str(m.get("name") or mid)})
    return out


def _extract_json_array(text: str, path: List[str]):
    try:
        value = json.loads(text)
    except ValueError:
        return None
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, list) else None


async def fetch_signed_get(url: str, ak: str, sk: str, security_token: str,
                           extra_unsigned_headers: Optional[Dict[str, str]] = None,
                           timeout: float = 15.0) -> Optional[str]:
    """签名 GET 并返回响应文本；失败返回 None（**不阻断调用方**）。

    `extra_unsigned_headers`（Agent-Type / X-Language）在**签名之后**追加 ——
    参与签名会 401（见 SNAP_UNSIGNED_HEADERS 的注释）。
    """
    if not ak or not sk:
        return None
    headers = signed_headers_for_request(
        ak, sk, security_token, "GET", url, b"",
        extra_unsigned_headers=extra_unsigned_headers)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            return None
        return r.text
    except Exception as e:
        logger.warning("codearts signed GET failed (%s): %s", url, e)
        return None


async def fetch_builtin_models(cred: dict) -> List[dict]:
    """`GET /v1/model/builtin`（签名 + 两个不参与签名的头）。"""
    text = await fetch_signed_get(
        f"{CODEARTS_SNAP_ACCESS_BASE}{CODEARTS_MODEL_BUILTIN_PATH}",
        str(cred.get("access_key_id") or ""),
        str(cred.get("secret_access_key") or ""),
        str(cred.get("security_token") or ""),
        extra_unsigned_headers=dict(SNAP_UNSIGNED_HEADERS))
    if text is None:
        return []
    return parse_builtin_models(_safe_json(text))


async def fetch_gateway_models(cred: dict) -> List[dict]:
    """`GET opengw /api/v1/gateway/config`（同样签名；无返回时静默空列表）。"""
    text = await fetch_signed_get(
        CODEARTS_OPENGW_GATEWAY_CONFIG_URL,
        str(cred.get("access_key_id") or ""),
        str(cred.get("secret_access_key") or ""),
        str(cred.get("security_token") or ""))
    if text is None:
        return []
    arr = _extract_json_array(text, ["result", "models"])
    if arr is None:
        return []
    seen: set = set()
    out = []
    for item in arr:
        entry = parse_model_entry(item, seen)
        if entry:
            out.append(entry)
    return out


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


async def fetch_remote_models(cred: dict) -> List[dict]:
    """双端点合并拉取（先 gateway 后 builtin）；失败返回空列表由调用方回退静态种子。"""
    gateway = await fetch_gateway_models(cred)
    builtin = await fetch_builtin_models(cred)
    return merge_models(gateway, builtin)


def model_context_window(model: str) -> Optional[int]:
    return CONTEXT_WINDOWS.get(str(model or ""))


def static_model_seed() -> List[dict]:
    return [{"id": m, "name": m} for m in DEFAULT_MODELS]


# ── 额度（statistics/plugin）──────────────────────────────

async def fetch_account_info(cred: dict) -> Tuple[Optional[dict], str]:
    """查询账户/套餐信息 → (info, error)。info 含**积分账户检测**。

    `package.is_credit_package === true` 即积分账户（IDE 前端正是用它决定渲染
    积分版还是 Token 版布局）。这是能否领积分的前置条件 —— Token 计费账户
    不在「每日签到得积分」活动范围内，直接尝试领取只会拿到语义模糊的错误。

    额度取 `metrics[]` 里 `usageTotalPackageCredit.package_credit_remain`，
    **不累加各分类**（分类是总额的构成明细，相加会重复计算）。

    失败原因必须**带出服务端原文**：`HTTP 401` 与 `APIG.0301 verify ak sk
    signature fail` 的处置方式完全不同，压成一句「查询失败」会让排查无门。
    """
    url = f"{CODEARTS_SNAP_ACCESS_BASE}{CODEARTS_PACKAGE_INFO_PATH}"
    data, _status, err = await _signed_request("GET", url, cred)
    if data is None:
        return None, err
    # `statistics/plugin` 是**裸对象**形态（无统一信封的 data 解包），
    # 但也兼容 `{code,data}` 信封（_signed_request 已统一处理两种）
    pkg = data.get("package") if isinstance(data.get("package"), dict) else {}
    name_cn = str(pkg.get("package_name_cn") or "")
    name_en = str(pkg.get("package_name_en") or "")
    return {
        "is_credit_package": pkg.get("is_credit_package") is True,
        "is_token_package": pkg.get("is_token_package") is True,
        "spec_code": str(pkg.get("spec_code") or ""),
        "package_name": name_cn or name_en,
        "package_status": str(pkg.get("status") or ""),
        "credit_remain": parse_credit_remain(data.get("metrics")),
    }, ""


def parse_credit_remain(metrics) -> Optional[float]:
    """从 metrics 取总额剩余积分；没有任何 credit metric 时返回 None。

    ⚠️ `None`（该账户没有积分口径）与 `0.0`（有口径但余额为 0）必须严格区分：
    把前者当 0 会让 Token 账户显示成「积分已用完」，误导用户去找并不存在的原因。
    """
    if not isinstance(metrics, list):
        return None
    labels = {
        "usageTotalPackageCredit", "usageBasicPackageCredit",
        "usageOnDemandPackageCredit", "usageBonusPackageCredit",
    }
    total_remain = None
    saw_any = False
    fallback_sum = 0.0
    for item in metrics:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name not in labels:
            continue
        saw_any = True
        remain = _num(item.get("package_credit_remain"))
        if name == "usageTotalPackageCredit":
            total_remain = remain
        else:
            fallback_sum += remain
    if not saw_any:
        return None
    # 总额缺失时才回退分类求和（有总额时相加会重复计算）
    return round(total_remain if total_remain is not None else fallback_sum, 2)


def _num(value, fallback: float = 0.0) -> float:
    try:
        n = float(value)
        return n if n == n else fallback
    except (TypeError, ValueError):
        return fallback


# ── 每日签到（四步：账户类型 → 活动预检 → claim → 必要时 confirm）──────

async def _signed_request(method: str, url: str, cred: dict,
                          body: Optional[str] = None) -> Tuple[Optional[dict], int, str]:
    """带签名的 snap-access 请求 → (parsed_or_None, status, detail)。

    错误体里的 `error_code/error_msg` **必须带出来**：`HTTP 401` 与
    `APIG.0301 verify ak sk signature fail` 的处置方式完全不同，
    压成前者会让「签名头位置不对 / 凭据过期 / AK 无权限」看起来一模一样。
    """
    ak = str(cred.get("access_key_id") or "")
    sk = str(cred.get("secret_access_key") or "")
    if not ak or not sk:
        return None, 0, "凭据缺少 AK/SK"
    payload = body.encode("utf-8") if body is not None else b""
    headers = signed_headers_for_request(
        ak, sk, str(cred.get("security_token") or ""), method, url, payload,
        extra_unsigned_headers=dict(SNAP_UNSIGNED_HEADERS))
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method.upper() == "GET":
                r = await client.get(url, headers=headers)
            else:
                r = await client.post(url, headers=headers, content=body or "")
        text = r.text if hasattr(r, "text") else ""
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {e}"
    if r.status_code >= 400:
        return None, r.status_code, _describe_http_failure(r.status_code, text)
    parsed = _safe_json(text)
    if not isinstance(parsed, dict):
        return None, r.status_code, "响应无法解析"
    if "code" in parsed and parsed.get("code") not in (0, None):
        msg = str(parsed.get("message") or parsed.get("msg") or parsed.get("code"))
        return None, r.status_code, msg
    inner = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
    return inner, r.status_code, ""


def _describe_http_failure(status: int, text: str) -> str:
    """带服务端原因的非 2xx 说明（对齐 codearts-credits.ts:223-235）。"""
    detail = ""
    parsed = _safe_json(text)
    if isinstance(parsed, dict):
        parts = [str(parsed.get("error_code") or ""), str(parsed.get("error_msg") or "")]
        detail = " ".join(p for p in parts if p)
    if not detail:
        detail = str(text or "").strip()[:200]
    return f"HTTP {status}：{detail}" if detail else f"HTTP {status}"


def read_identifier(source: dict, key: str) -> str:
    """读**标识符类**字段并统一成字符串（兼容数字与字符串两种形态）。

    ⚠️ 服务端对同一语义字段的类型并不一致：`campaignId` 下发的是**数字** `1`，
    而活动 `status` 是字符串（不可领取时甚至为 `null`）。用只接受字符串的读法
    会得到空串，领取被判「活动缺少 campaignId」而失败——用户看到「1 个失败」
    但积分其实没领到。
    """
    if not isinstance(source, dict):
        return ""
    v = source.get(key)
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def read_number(source: dict, key: str, fallback: float = 0.0) -> float:
    if not isinstance(source, dict):
        return fallback
    v = source.get(key)
    if isinstance(v, bool):
        return fallback
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(v)
        except ValueError:
            return fallback
    return fallback


def parse_activity(item) -> dict:
    """解析活动列表的一项（`codearts-credits.ts:487-501`）。"""
    if not isinstance(item, dict):
        item = {}
    return {
        # campaignId 可能是数字型 → 必须用 read_identifier
        "campaign_id": read_identifier(item, "campaignId"),
        "type": str(item.get("type") or ""),
        "title": str(item.get("title") or ""),
        "claimable": item.get("claimable") is True,
        # status 不可领取时可能为 null → 统一成字符串（避免 None 参与 join 报错）
        "status": str(item.get("status") or ""),
        # ⚠️ 字段名是 benefitAmount（实测 1000），不是 amount
        "amount": (read_number(item, "benefitAmount")
                   or read_number(item, "amount")
                   or read_number(item, "creditAmount")),
    }


def find_daily_checkin_activity(activities: List[dict]) -> Optional[dict]:
    """找「每日登录领取」那一项；INVITE_USER / NEW_USER_REGISTER /
    STUDENT_CERTIFIED 不属于每日签到，不能混领。"""
    for a in activities or []:
        if isinstance(a, dict) and a.get("type") == CODEARTS_DAILY_LOGIN_TYPE:
            return a
    return None


# `status` 里表示「已领取/已确认/已核销」的取值（IDE 在这三态禁用领取按钮）
CLAIMED_STATUSES = ("CLAIMED", "CONFIRMED", "CONSUMED")


async def fetch_ops_activities(cred: dict) -> Tuple[Optional[List[dict]], str]:
    """活动列表：`GET /v1/ops/delivery?channel=IDE`。"""
    url = (f"{CODEARTS_SNAP_ACCESS_BASE}{CODEARTS_OPS_DELIVERY_PATH}"
           f"?channel={CODEARTS_OPS_CHANNEL}")
    data, _st, err = await _signed_request("GET", url, cred)
    if data is None:
        return None, err
    items = data.get("items")
    if not isinstance(items, list):
        return None, "响应缺少 items 字段"
    return [parse_activity(i) for i in items], ""


async def claim_daily_checkin(cred: dict) -> dict:
    """执行每日签到（四步，失败与业务状态分别表达）。

    返回 `{"kind": ..., "credit": float, "message": str, "error": str}`，
    `kind` 取值与仓库既有的 ClaimOutcome 对齐（claimed / already_claimed /
    inactive / failed）。

    判定顺序（每一步对应**对用户含义不同**的结果）：
    1. 账户类型查询失败 → failed；非积分账户 → inactive
       （活动范围限定「已升级到积分计费模式的用户」，Token 账户不该被报成失败）；
    2. 活动列表查询失败 → failed；无 USER_LOGIN 活动 → inactive；
    3. 不可领取且 status ∈ CLAIMED_STATUSES → already_claimed；其余不可领取 → inactive；
    4. `POST /v1/ops/claim` 失败 → failed；
    5. 响应 `id !== null` 时补 `POST /v1/ops/confirm`
       （漏掉会让积分停在「待确认」而不入账）；
    6. 成功 → claimed。

    第 3 步是**唯一的幂等保护**：本协议没有幂等键，也没有「今天已签到」业务码
    可依赖，预检不能省。
    """
    info, err = await fetch_account_info(cred)
    if info is None:
        return {"kind": "failed", "credit": 0.0,
                "message": f"账户信息查询失败：{err}", "error": err}
    if not info.get("is_credit_package"):
        return {"kind": "inactive", "credit": 0.0,
                "message": ("Token 计费账户，不在积分活动范围"
                            if info.get("is_token_package")
                            else "非积分计费账户，不在积分活动范围"),
                "error": ""}
    activities, err = await fetch_ops_activities(cred)
    if activities is None:
        return {"kind": "failed", "credit": 0.0,
                "message": f"活动列表查询失败：{err}", "error": err}
    activity = find_daily_checkin_activity(activities)
    if activity is None:
        return {"kind": "inactive", "credit": 0.0,
                "message": "未找到每日签到活动", "error": ""}
    if not activity.get("claimable"):
        if activity.get("status") in CLAIMED_STATUSES:
            return {"kind": "already_claimed", "credit": 0.0,
                    "message": "今天已领取", "error": ""}
        return {"kind": "inactive", "credit": 0.0,
                "message": f"当前不可领取（status={activity.get('status')}）", "error": ""}
    campaign_id = str(activity.get("campaign_id") or "")
    if not campaign_id:
        return {"kind": "failed", "credit": 0.0,
                "message": "活动缺少 campaignId，无法领取", "error": ""}

    claim_body = json.dumps({"campaignId": campaign_id,
                             "channel": CODEARTS_OPS_CHANNEL}, ensure_ascii=False)
    data, _st, err = await _signed_request(
        "POST", f"{CODEARTS_SNAP_ACCESS_BASE}{CODEARTS_OPS_CLAIM_PATH}",
        cred, claim_body)
    if data is None:
        return {"kind": "failed", "credit": 0.0,
                "message": f"领取失败：{err}", "error": err,
                "upstream_code": _st}

    # 服务端要求确认时才补 confirm（判据是响应的 id 非 null）。
    # ⚠️ confirm 失败**不**把整体判为失败：积分已进入待确认态，报 failed 会让
    # 用户以为没领到而重复点击；如实返回 claimed，把异常留在 error 字段。
    benefit_id = data.get("id")
    confirm_error = ""
    if benefit_id is not None:
        _d, _s, cerr = await _signed_request(
            "POST", f"{CODEARTS_SNAP_ACCESS_BASE}{CODEARTS_OPS_CONFIRM_PATH}",
            cred, json.dumps({"campaignId": campaign_id}, ensure_ascii=False))
        if _d is None:
            confirm_error = f"confirm 失败（积分可能停在待确认）：{cerr}"
            logger.warning("codearts confirm failed: %s", cerr)

    claim_credit = (read_number(data, "benefitAmount")
                    or read_number(data, "credit")
                    or read_number(data, "credits")
                    or read_number(data, "creditAmount")
                    or read_number(data, "amount"))
    credit = claim_credit if claim_credit > 0 else float(activity.get("amount") or 0)
    return {"kind": "claimed", "credit": round(credit, 2),
            "message": "签到成功" if not confirm_error else "签到成功（待确认）",
            "error": confirm_error, "activity_name": str(activity.get("title") or "")}


# ── 凭据 → 出站请求头（适配器用）────────────────────────────

def chat_url() -> str:
    return f"{CODEARTS_SNAP_ACCESS_BASE}{CODEARTS_CHAT_PATH}"


def build_chat_headers(cred: dict, model: str, body: bytes,
                       chat_id: str = "", session_id: str = "",
                       now: Optional[float] = None) -> Dict[str, str]:
    """chat/completions 的出站头（签名 + 固定业务头）。

    ⚠️ `body` 必须是**真实发出的字节**：`x-sdk-content-sha256` 是它的摘要，
    签名也基于它。传空字节（或先序列化两次）都会验签失败。

    `maas_type: benefit` 走 `extra_signed_headers`（参与签名）——
    这与 `Agent-Type` 必须**不**参与签名是同一类坑的两面，别弄反。
    """
    headers = signed_headers_for_request(
        str(cred.get("access_key_id") or ""),
        str(cred.get("secret_access_key") or ""),
        str(cred.get("security_token") or ""),
        "POST", chat_url(), body,
        extra_signed_headers=maas_type_headers(model), now=now)
    if chat_id:
        headers["Chat-Id"] = chat_id
    if session_id:
        headers["Session-Id"] = session_id
    headers["lang"] = "en"
    return headers


def new_chat_id() -> str:
    return _uuid.uuid4().hex


def is_credential_expired(cred: dict, skew_seconds: int = 60) -> bool:
    """凭据是否已过期（含时钟偏差余量）；`expires_at` 缺失时**视为不过期**。

    缺失时不当作过期是刻意的：旧 ticket 凭据可能没有 expiration，
    判过期会让每次请求都去刷新，而刷新又必然失败（没有 refresh_token）。
    """
    raw = str(cred.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds() <= int(skew_seconds)


def user_facing_provider_name() -> str:
    return "CodeArts Agent"


def parse_iso_expires_at(raw: str) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
