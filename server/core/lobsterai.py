"""LobsterAI（有道龙虾）协议实现。

协议来源：Jet-Hub 源码逐行核对（github.com/zhengwuji/Jet-Hub，MIT）
+ 2026-09 生产实测。本模块只承载**协议细节**，登录编排在 oauth_client。

## 与其它渠道的差异（全部是实测结论，不是推测）

1. **无 PKCE / 无 DPoP / 无签名** —— exchange 与 refresh 都不需要。
2. **uuid / first_keyfrom 由客户端生成**且不在服务端响应里，必须随凭据持久化；
   续期时原样回传，否则续期失败只能重登（`lobsterai.ts:85-104` 的教训）。
3. **latest_keyfrom 刻意不更新**：虽然字段名叫「最近活动」，但 Go 参考实现
   （`auth.go:37-50`）每次续期发的都是**登录时的那一刻**，本实现照做。
4. **version 是动态真值**：从 api-overmind 更新接口拉（12h 缓存），取不到时
   用兜底值继续（参考实现 `sigin.py:73-76` 是直接放弃，本实现更宽容 ——
   反证：Go 侧一直发假值 `0.1.0` 也未失败）。
5. **`stream` 恒为 true**：上游只支持 SSE，`stream:false` 返回 500。
6. **模型列表的 query 是身份载荷**（keyfrom），发错身份会让服务端返回错误的
   模型集合；且必须带 `X-LobsterAI-Client-Capabilities` 头，否则少 `kimi-k3`。
7. **思考档位的 wire 值是 `openclawLevel` 而非 `level`**：远端把 `level:max`
   映射为 `openclawLevel:xhigh`；直接发 `max` 与不带参数无差异。
8. **SSE 的 `delta.content` / `delta.reasoning_content` 会显式返回 null** ——
   必须 `typeof === 'string'` 判定（openai_compat 的 `if content:` 天然安全）。
9. **不支持图片**：`inputModalities` 恒为 `['text']`。
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Dict, Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# ── 常量（照抄 Jet-Hub lobsterai.ts / lobsterai-product.ts）──
LOBSTERAI_API_BASE = "https://lobsterai-server.youdao.com"
LOBSTERAI_PORTAL_BASE = "https://lobsterai.youdao.com"
LOBSTERAI_EXCHANGE_PATH = "/api/auth/exchange"
LOBSTERAI_REFRESH_PATH = "/api/auth/refresh"
LOBSTERAI_MODELS_PATH = "/api/models/available"
LOBSTERAI_CHAT_PATH = "/api/proxy/v1/chat/completions"
LOBSTERAI_CALLBACK_PATH = "/auth/callback"
LOBSTERAI_CLIENT_VERSION_API = (
    "https://api-overmind.youdao.com/openapi/get/luna/hardware/lobsterai/prod/update"
)
LOBSTERAI_FALLBACK_CLIENT_VERSION = "2026.9.4"
LOBSTERAI_CLIENT_CAPABILITIES = "kimi-k3-agentic-v1,thinking-level-control-v1"
LOBSTERAI_USER_AGENT = "LobsterAI/0.1.0"

# 版本号缓存（12h，照抄 Jet-Hub 的 clientVersion 缓存语义）
_VERSION_CACHE: Tuple[float, str] = (0.0, "")
_VERSION_TTL = 12 * 3600


def parse_client_version(raw) -> Optional[str]:
    """校验日期式版本号（主干为点分数字 + 可选预发布后缀）。

    校验而非直接采信：版本号是 chat/models 的必填参数，若上游返回 null /
    HTML 错误页，把它拼进请求会以一个更费解的错误失败（对齐 sigin.py:27-28）。
    """
    import re
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not re.match(r"^(\d+(?:\.\d+)*)(?:-[0-9A-Za-z.-]+)?$", s):
        return None
    return s


def parse_client_version_from_update(body) -> Optional[str]:
    """从更新接口响应里取 `data.value.version`（该响应**不是**统一信封）。"""
    if not isinstance(body, dict):
        return None
    outer = body.get("data")
    if not isinstance(outer, dict):
        return None
    value = outer.get("value")
    if not isinstance(value, dict):
        return None
    return parse_client_version(value.get("version"))


async def resolve_client_version(force: bool = False) -> str:
    """动态拉取客户端版本号（12h 缓存）；失败回退兜底值。

    永不抛异常 —— 版本号只是请求参数，取不到不该阻塞登录/续期。
    """
    global _VERSION_CACHE
    now = time.monotonic()
    ts, ver = _VERSION_CACHE
    if not force and ver and ts > now:
        return ver
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(LOBSTERAI_CLIENT_VERSION_API,
                                 headers={"Accept": "application/json",
                                          "User-Agent": LOBSTERAI_USER_AGENT})
        if r.is_success:
            v = parse_client_version_from_update(r.json())
            if v:
                _VERSION_CACHE = (now + _VERSION_TTL, v)
                return v
    except Exception as e:
        logger.warning("lobsterai client version fetch failed: %s", e)
    # 兜底：宁可用稍旧的版本号试，也别让用户完全无法登录
    return ver or LOBSTERAI_FALLBACK_CLIENT_VERSION


def anonymous_headers() -> Dict[str, str]:
    """exchange / refresh 用（这两个端点不需要 Authorization）。"""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": LOBSTERAI_USER_AGENT,
    }


def build_keyfrom_body(cred: dict, client_version: str) -> Dict[str, str]:
    """构造 keyfrom 身份载荷（续期与模型列表共用）。

    `latestKeyfrom` 用凭据里**存储的原值**（不取当前时刻）—— 严格对齐
    Go 的 `KeyfromBody()`。uuid / userId 缺失时**删键**而非写空串。
    """
    body: Dict[str, str] = {
        "firstKeyfrom": str(cred.get("first_keyfrom") or ""),
        "latestKeyfrom": str(cred.get("latest_keyfrom") or ""),
        "version": client_version,
    }
    uuid = str(cred.get("uuid") or "")
    if uuid:
        body["uuid"] = uuid
    uid = str(cred.get("user_id") or "")
    if uid:
        body["userId"] = uid
    return body


def build_login_url(port: int, state: str) -> str:
    """构造 portal 登录 URL（形态照抄 main.go:225-228）。

    redirect_uri **必须**是 `http://127.0.0.1:{port}/auth/callback` 形态 ——
    登录页会校验（main.go:223-225 的注释记录了这一约束）。hash 段（`#/login`）
    属于 fragment，不能用 searchParams 构造，故显式拼装。
    """
    redirect_uri = f"http://127.0.0.1:{port}{LOBSTERAI_CALLBACK_PATH}"
    query = (f"source=electron"
             f"&redirect_uri={quote(redirect_uri, safe='')}"
             f"&state={quote(state, safe='')}")
    return f"{LOBSTERAI_PORTAL_BASE}/portal#/login?{query}"


def parse_token_payload(data: dict) -> dict:
    """解析 exchange / refresh 响应里的令牌与用户信息。"""
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    expires_in = data.get("expiresIn")
    return {
        "access_token": str(data.get("accessToken") or ""),
        "refresh_token": str(data.get("refreshToken") or ""),
        "expires_in": int(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else None,
        "user_id": str(user.get("id") or ""),
        "yid": str(user.get("yid") or ""),
        "account_user_id": str(user.get("userId") or ""),
        "nickname": str(user.get("nickname") or ""),
    }


def resolve_uid(payload: dict) -> str:
    """账号唯一 ID，四级回退：user.id → user.userId → user.yid → sha256(token)[:16]。

    末级哈希兜底保证任何情况下都有稳定 ID（否则空 uid 会让多账号互相覆盖）。
    与 Go 的 `fmt.Sprintf("%x", sha256.Sum256(...))[:16]` 完全一致。
    """
    for key in ("user_id", "account_user_id", "yid"):
        v = str(payload.get(key) or "")
        if v:
            return v
    tok = str(payload.get("access_token") or "")
    return hashlib.sha256(tok.encode()).hexdigest()[:16]


def parse_envelope(body) -> Tuple[bool, str, dict]:
    """统一信封 `{code,msg,data}` → (ok, message, data)。

    chat 端点例外（返回裸 SSE 不套信封），故只在 exchange/refresh 用。
    """
    if not isinstance(body, dict):
        return False, "响应不是 JSON 对象", {}
    code = body.get("code")
    msg = str(body.get("msg") or body.get("message") or "")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if code not in (0, None):
        return False, msg or f"code={code}", data
    if not data:
        # code=0 但 data 为空：sigin.py:46-47 用它判定「accessToken 可能已失效」。
        # 不沿用 msg（此时 msg 常是 "OK"，对排障没有信息量）
        return False, "data 为空（accessToken 可能已失效，请重新登录）", {}
    return True, msg, data


async def exchange_auth_code(code: str, session: dict, client_version: str,
                             ) -> Tuple[Optional[dict], str]:
    """用授权码换凭据 → (credential_dict, error)。

    body **必须**含 5 个字段（对齐 main.go:264-270）：
    authCode / firstKeyfrom / latestKeyfrom / uuid / version。
    """
    body = {
        "authCode": code,
        "firstKeyfrom": session.get("first_keyfrom", ""),
        "latestKeyfrom": str(int(time.time() * 1000)),
        "uuid": session.get("uuid", ""),
        "version": client_version,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{LOBSTERAI_API_BASE}{LOBSTERAI_EXCHANGE_PATH}",
                                  headers=anonymous_headers(), json=body)
    except Exception as e:
        return None, f"exchange 网络失败：{type(e).__name__}"
    try:
        parsed = r.json()
    except Exception:
        return None, f"exchange 响应不是 JSON（HTTP {r.status_code}）"
    ok, msg, data = parse_envelope(parsed)
    if not ok:
        return None, f"exchange 失败：{msg}"
    payload = parse_token_payload(data)
    if not payload["access_token"]:
        return None, "exchange 响应缺少 accessToken"
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_in": payload["expires_in"] or 3600,
        "user_id": resolve_uid(payload),
        "nickname": payload["nickname"],
        # 身份字段：随凭据持久化，续期时原样回传
        "uuid": session.get("uuid", ""),
        "first_keyfrom": session.get("first_keyfrom", ""),
        "latest_keyfrom": body["latestKeyfrom"],
        "client_version": client_version,
    }, ""


async def refresh_token(cred: dict) -> Tuple[Optional[dict], str]:
    """续期 → (new_credential_partial, error)。

    请求体 = keyfrom 身份载荷 + refreshToken（**缺 keyfrom 必失败**）。
    ⚠️ latestKeyfrom 用凭据里存储的原值，**刻意不更新为当前时刻**。
    """
    refresh_plain = str(cred.get("refresh_token") or "")
    if not refresh_plain:
        return None, "no refresh_token stored"
    version = str(cred.get("client_version") or "") or await resolve_client_version()
    body = dict(build_keyfrom_body(cred, version))
    body["refreshToken"] = refresh_plain
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{LOBSTERAI_API_BASE}{LOBSTERAI_REFRESH_PATH}",
                                  headers=anonymous_headers(), json=body)
    except Exception as e:
        return None, f"refresh 网络失败：{type(e).__name__}"
    if r.status_code >= 400:
        return None, f"refresh HTTP {r.status_code}: {r.text[:200]}"
    try:
        parsed = r.json()
    except Exception:
        return None, "refresh 响应不是 JSON"
    ok, msg, data = parse_envelope(parsed)
    if not ok:
        return None, f"refresh 失败：{msg}"
    payload = parse_token_payload(data)
    if not payload["access_token"]:
        return None, "refresh 响应缺少 accessToken"
    return {
        "access_token": payload["access_token"],
        # refresh 响应可能不返回新 refreshToken（沿用旧的），不能覆盖成空串
        "refresh_token": payload["refresh_token"] or refresh_plain,
        "expires_in": payload["expires_in"] or 3600,
        # 身份字段一律沿用旧值（latest_keyfrom 尤其不能更新）
        "uuid": cred.get("uuid", ""),
        "first_keyfrom": cred.get("first_keyfrom", ""),
        "latest_keyfrom": cred.get("latest_keyfrom", ""),
        "client_version": version,
    }, ""


def parse_models(body) -> list:
    """解析 `GET /api/models/available`（统一信封 + data 数组，兼容嵌套一层）。"""
    if not isinstance(body, dict):
        return []
    if body.get("code") not in (0, None):
        return []
    data = body.get("data")
    raw = data if isinstance(data, list) else (
        data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else []
    )
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("modelId") or "")
        if not mid:
            continue
        entry = {
            "id": mid,
            "name": str(item.get("modelName") or mid),
        }
        ctx = item.get("contextWindow")
        if isinstance(ctx, (int, float)) and ctx > 0:
            entry["context_length"] = int(ctx)
        if isinstance(item.get("supportsImage"), bool):
            entry["supports_vision"] = item["supportsImage"]
        if isinstance(item.get("maxTokens"), (int, float)) and item["maxTokens"] > 0:
            entry["max_output_tokens"] = int(item["maxTokens"])
        cm = item.get("costMultiplier")
        if isinstance(cm, (int, float)) and cm > 0:
            entry["cost_multiplier"] = float(cm)
        out.append(entry)
    return out


async def fetch_models(cred: dict) -> list:
    """拉取远端模型列表（query 是身份载荷，必须带客户端能力头）。

    失败返回空列表，由调用方回退静态种子（与 Jet-Hub 同语义）。
    """
    version = str(cred.get("client_version") or "") or await resolve_client_version()
    params = build_keyfrom_body(cred, version)
    headers = {
        "Accept": "application/json",
        "User-Agent": LOBSTERAI_USER_AGENT,
        "Authorization": f"Bearer {cred.get('access_token', '')}",
        "X-LobsterAI-Client-Capabilities": LOBSTERAI_CLIENT_CAPABILITIES,
        "X-LobsterAI-Client-Version": version,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{LOBSTERAI_API_BASE}{LOBSTERAI_MODELS_PATH}",
                                 params=params, headers=headers)
        if not r.is_success:
            return []
        return parse_models(r.json())
    except Exception as e:
        logger.warning("lobsterai fetch_models failed: %s", e)
        return []
