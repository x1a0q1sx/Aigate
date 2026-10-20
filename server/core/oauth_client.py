"""
OAuth Client — AIGate 端 OAuth 2.0 客户端核心

职责：
  1) 构造 authorize_url（含 PKCE code_verifier / code_challenge / state）
  2) 用 code 换 access_token（依 provider 配置）
  3) proactively refresh access_token（提前置 + 单飞 Single Flight 锁）
  4) persist（encrypt）token 到 DB
  5) pick_token_for_provider：给 v1_router 用，自动判断是否需要刷新

实现核心点：
  - 不依赖 authlib — httpx 直接 POST OAuth token endpoint
  - Single Flight：相同 provider_code 同时刷新，只有一个请求真正去 OAuth，其它等待复用结果
  - PKCE：每次authorize生成 code_verifier，state 用 random hex
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import httpx
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from sqlalchemy import select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from server.db import AsyncSessionLocal
from server.models.oauth_token import OAuthToken
from server.core.crypto_service import get_crypto_service, CryptoService
from server.core.error_text import is_risk_block_text
from server.core.quota_limit import is_daily_limit_text, is_credit_exhausted_text
from server.core.oauth_registry import OAuthProviderConfig, get_oauth_provider

logger = logging.getLogger(__name__)

# 账号级脏标记：某账号失败过（冷却写入 DB）才置位；成功清理时只有脏账号
# 才查库写库 —— 让正常成功路径保持零 DB 开销（多账号冷却的常用情形）。
_ACCOUNT_DIRTY: set = set()

# 设备流终态记录（进程内）：muse 轮询在后台跑，失败原因（浏览器未批准/
# expired_token/铸 key 失败/被 WAF 拦）此前只进服务端日志，前端 6 分钟
# 超时前完全无感知。此处把终态暴露给 GET /admin/oauth/device-status。
# key = provider_code，value = {"state": 设备码, "status": ok|failed|pending,
#   "error": 文案, "at": epoch 秒}；仅保留最近一次设备流。
_DEVICE_FLOW_STATUS: dict = {}


def record_device_flow(provider_code: str, state: str, status: str,
                       error: str = "", detail: str = "") -> None:
    """记录设备流终态（供前端实时展示失败原因）。

    detail 用于成功态补充说明（如 CodeBuddy 同账号重登：token 写回已有连接，
    连接数不增加 —— 前端据此提示「已更新原连接」而不是空等 6 分钟）。
    """
    _DEVICE_FLOW_STATUS[provider_code] = {
        "state": state,
        "status": status,
        "error": str(error or "")[:500],
        "detail": str(detail or "")[:300],
        "at": time.time(),
    }


def get_device_flow_status(provider_code: str) -> dict:
    return dict(_DEVICE_FLOW_STATUS.get(provider_code) or {})

_STATUS_CODE_RE = re.compile(r"(?<![\d.])([45]\d{2})(?![\d.])")


def status_from_error_text(text) -> Optional[int]:
    """从错误文案里提取 HTTP 状态码（429/401/403/5xx…），提取不到返回 None。

    用于账号级冷却的分级（429=短冷却、401/403=长冷却）：上游错误在网关内
    以字符串形态流转，此处是仅有的结构化兜底。"""
    m = _STATUS_CODE_RE.search(str(text or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _now_utc() -> datetime:
    return datetime.utcnow()


def _force_proxy_kwargs() -> dict:
    """OAuth 流程里访问**按国别封锁**的上游（Meta 等）时的 httpx 代理参数。

    服务端直连不通、必须经 mihomo；走代理池并 force=True（池 enabled=False 时
    仅 force 生效，语义即「服务商/流程强制走代理」，与推理侧 __proxy_force 一致）。
    池空/全冷却时返回 {} = 静默回落直连。
    """
    try:
        from server.core.proxy_pool import get_proxy_pool
        return get_proxy_pool().proxied_kwargs(force=True)
    except Exception:
        return {}


# Meta Muse 设备流统一 UA（照搬 CLIProxyAPI 55.5k★ 的取值）：
# auth.meta.com 对无 UA / 非官方 UA 的容忍度不稳定，官方 CLI 指纹最稳。
MUSE_USER_AGENT = "muse-code/1.0.2"


# P1-15: 判定"凭证真的失效"（该永久下线）vs "上游抖动"（只记错误，保留 active）。
# 此前任意 >=400 都 is_active=False，上游一次 429/502/超时就把连接判死刑，
# 调度器只扫 active 连接 → 须人工重新登录才能恢复。
_DEAD_CREDENTIAL_MARKERS = (
    "invalid_grant", "invalid_token", "invalid refresh", "refresh token has expired",
    "refresh_token expired", "token has been revoked", "unauthorized_client",
    "refresh token is invalid", "登录已过期", "凭证无效",
)


def _refresh_credential_dead(status_code: int, error_text: str = "") -> bool:
    """True = refresh token 确实失效（应下线）；False = 临时故障（保留 active 待重试）。

    规则：401 恒为失效；400 需带 invalid_grant 类语义标记；
    429/5xx/网络错误一律视为临时（保留下线机会）。
    """
    text = (error_text or "").lower()
    if status_code == 401:
        return True
    if status_code == 400:
        return any(m in text for m in _DEAD_CREDENTIAL_MARKERS)
    if status_code in (403, 404):
        # 403 可能是风控/权限而非失效；404 端点变更。只有明确标记才下线。
        return any(m in text for m in _DEAD_CREDENTIAL_MARKERS)
    # 429 限流、5xx 上游故障、其它 → 临时
    return False


# ── 凭证身份（防止 A 账号的 token 覆盖 B 账号的连接）──────────────
# 事故（2026-09-22，实测取证）：`一键连接` 走 device poll 时，轮询协程在拿到
# token 前先按当时库里已有的 owner 分配（如 __default）；而用户是在**另一个
# 账号**上完成登录。结果第二次登录把第一个账号的连接覆盖成了新账号的 token ——
# 两条连接的 token 完全一致（sha1 相同）、JWT 的 sub 也相同，于是「两个账号」
# 显示同一份额度，第一个账号的凭据被静默丢弃（只能从备份里捞回来）。
# 这里做**写入侧身份守卫**：新 token 能解出身份、且与该行已有 token 的身份
# 不同 → 拒绝覆盖并报错，由调用方另建 owner（account-N）。
_IDENTITY_CLAIM_KEYS = ("preferred_username", "email", "sub")


def _jwt_claims(token: str) -> Optional[dict]:
    """解码 JWT payload（只读、不验签）；非 JWT / 解析失败返回 None。"""
    if not token or token.count(".") < 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8", "replace"))
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def token_identity(token: str) -> Optional[str]:
    """从 JWT 形态的 token 里取账号标识；非 JWT / 无身份字段返回 None。

    只读 payload，**不校验签名**（仅用于「是不是同一个账号」的比对，
    不承担鉴权职责）。取不到就返回 None = 身份未知 → 不参与守卫。
    """
    claims = _jwt_claims(token)
    if not claims:
        return None
    for key in _IDENTITY_CLAIM_KEYS:
        v = claims.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def default_owner_name(token: str) -> Optional[str]:
    """新连接的默认账号名（2026-10-13 用户要求）：优先 email（Intl），
    其次 preferred_username（CN 即手机号）。都取不到返回 None（保留自动分配名）。

    owner 是路由寻址键也是展示名；仅用于**新账号**的默认命名 —— 已存在的
    连接名（含用户手动改名）绝不因本函数改动。长度截到 100（owner 列宽）。
    """
    claims = _jwt_claims(token)
    if not claims:
        return None
    for key in ("email", "preferred_username"):
        v = claims.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:100]
    return None


def _is_auto_allocated_owner(owner: str) -> bool:
    """owner 是否为系统自动分配名（__default / account-N）——用户要求默认名
    用邮箱时，只替换自动名，绝不改动用户手动改过的名字。"""
    if not owner:
        return False
    if owner == "__default":
        return True
    if owner.startswith("account-"):
        return owner[8:].isdigit()
    return False


def gen_pkce_pair() -> Tuple[str, str]:
    """生成 (code_verifier, code_challenge) — S256 method"""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def gen_state() -> str:
    return secrets.token_urlsafe(32)


def _expires_in_from(iso_exp, fallback) -> int:
    """expiresAt ISO 字符串 → 剩余秒数；无则用 fallback 秒数（下限 60s）。"""
    try:
        if iso_exp:
            dt = datetime.fromisoformat(str(iso_exp).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(60, int((dt - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        pass
    try:
        return max(60, int(fallback or 3600))
    except Exception:
        return 3600


def decode_cline_code(code: str) -> Optional[dict]:
    """Cline 登录回调的 code = base64(JSON token)。

    官方扩展实现：授权跳转回来的 code 参数直接编码了 token 数据（补 = 填充、
    容忍尾部垃圾字符，取首个 { 到最后一个 } 的 JSON）。返回 _save_token 可用的
    标准 tok dict；任何解析失败返回 None（调用方走 POST 换票兜底）。
    """
    import json
    import base64
    try:
        b64 = (code or "").strip()
        if not b64:
            return None
        pad = 4 - len(b64) % 4
        if pad != 4:
            b64 += "=" * pad
        decoded = base64.b64decode(b64).decode("utf-8", errors="replace")
        start, end = decoded.find("{"), decoded.rfind("}")
        if start < 0 or end <= start:
            return None
        t = json.loads(decoded[start:end + 1])
        access = t.get("accessToken")
        if not access:
            return None
        return {
            "access_token": access,
            "refresh_token": t.get("refreshToken") or "",
            "expires_in": _expires_in_from(t.get("expiresAt"), 3600),
            "token_type": "Bearer",
            "scope": ("email:%s" % t["email"]) if t.get("email") else "cline",
        }
    except Exception:
        return None


class OAuthClient:
    """OAuth 客户端 + 主动刷新 + 持久化"""

    def __init__(self, crypto: CryptoService = None, redirect_override: str = None):
        self._crypto = crypto or get_crypto_service()
        self.redirect_override = redirect_override         # 运行时 host 替换默认 redirect
        self._flight_locks: Dict[str, asyncio.Lock] = {}    # Single Flight per (provider+owner)
        self._inflight: Dict[str, asyncio.Future] = {}       # 进行中 refresh 的 future
        # cline 类「回调不带 state」的 provider：记住 authorize 时下发的 redirect_uri / 会话
        self._pending_redirect: Dict[str, str] = {}
        self._pending_sessions: Dict[str, str] = {}          # provider_code → packed_state
        # LobsterAI：state → {uuid, first_keyfrom, owner}（回调时凭 state 找回会话）
        self._lobsterai_sessions: Dict[str, dict] = {}
        # Trae：state → {machine_id, device_id, owner, domain}
        self._trae_sessions: Dict[str, dict] = {}
        # CodeArts：state → {port, code_verifier, dpop_jwk, ticket_id, owner}
        # ⚠️ code_verifier 与 DPoP 私钥必须存下来：续期时两者都要重发
        self._codearts_sessions: Dict[str, dict] = {}
        # Freebuff：state → {info, owner, created_at}（CLI 授权码轮询，无回调）
        self._freebuff_sessions: Dict[str, dict] = {}

    # ── 唯一性 ──
    def _key(self, provider_code: str, owner: str = "__default") -> str:
        return f"{provider_code}::{owner}"

    # ── authorize URL ──
    def build_authorize_url(self, provider: OAuthProviderConfig, owner: str = "__default"
                            ) -> Tuple[str, str, Optional[str]]:
        """
        构造浏览器授权 URL。
        返回 (url, state, code_verifier)
        - PKCE provider：返回 code_verifier（callback 时需要）
        - device_code provider：返回 device_user_code（用户在另一页面输入）
        """
        redirect_uri = self.redirect_override or provider.redirect_uri
        state = gen_state()
        # 在 state 中编码 provider_code 和 owner，回调时反查 — HMAC-style 不验签，省事
        packed_state = f"{provider.code}|{owner}|{state}"
        # ── Cline 定制授权 URL：无 client_id/scope，参数为 client_type + callback_url/redirect_uri ──
        # （9router cline provider 同构；另带 state 以便回调透传，不带也不影响 token 解码）
        if (provider.extra_params or {}).get("auth_mode") == "cline":
            from urllib.parse import quote
            ep = provider.extra_params or {}
            ct = quote(ep.get("client_type", "extension"), safe="")
            ru = quote(redirect_uri, safe="")
            url = (f"{provider.authorize_url}?client_type={ct}"
                   f"&callback_url={ru}&redirect_uri={ru}&state={quote(packed_state, safe='')}")
            self._pending_redirect[provider.code] = redirect_uri
            self._pending_sessions[provider.code] = packed_state
            return url, packed_state, None
        query_pairs = {
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": packed_state,
            "scope": provider.scope,
        }
        params = []
        code_verifier = None
        if provider.use_pkce:
            code_verifier, code_challenge = gen_pkce_pair()
            # 保存 verifier 到内存（state → verifier），callback 时取回
            self._state_verifier_map[packed_state] = code_verifier
            params.append(("code_challenge", code_challenge))
            params.append(("code_challenge_method", "S256"))
        for k, v in query_pairs.items():
            if v:
                params.append((k, v))
        if provider.extra_params:
            for k, v in provider.extra_params.items():
                params.append((k, str(v)))
        # device_code 流不构造 authorize_url
        if not provider.authorize_url:
            return "", packed_state, None
        url = f"{provider.authorize_url}?{'&'.join(f'{k}={v}' for k, v in params)}"
        return url, packed_state, code_verifier

    # ── callback 处理 ──
    async def exchange_code_for_token(
        self,
        provider_code: str,
        code: str,
        state: str,
        db: AsyncSession,
    ) -> Tuple[bool, str, Optional[OAuthToken]]:
        """
        oauth callback：用 code + verifier 换 access_token
        state 格式：provider_code|owner|random
        """
        try:
            provider_code, owner, rand_state = state.split("|", 2)
        except Exception:
            return False, "invalid state format", None
        provider = get_oauth_provider(provider_code)
        if not provider:
            return False, f"unknown provider {provider_code}", None
        # ── Cline：回调 code 本身就是 base64(JSON) token，无需服务端换票 ──
        if (provider.extra_params or {}).get("token_in_code"):
            return await self._complete_token_in_code(provider, owner, code, db)
        verifier = self._state_verifier_map.pop(state, None)
        if provider.use_pkce and not verifier:
            return False, "missing PKCE verifier (state expired)", None
        token_url = provider.token_url
        post_data = {
            "grant_type": "authorization_code",
            "client_id": provider.client_id,
            "code": code,
            "redirect_uri": self.redirect_override or provider.redirect_uri,
        }
        if provider.client_secret:
            post_data["client_secret"] = provider.client_secret
        if provider.use_pkce and verifier:
            post_data["code_verifier"] = verifier
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(token_url, data=post_data,
                                      headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            return False, f"token endpoint HTTP {resp.status_code}: {resp.text[:300]}", None
        try:
            tok = resp.json()
        except Exception:
            return False, "invalid JSON response", None
        if "access_token" not in tok:
            return False, f"missing access_token: {tok.get('error_description') or tok.get('error') or 'unknown'}", None
        # 持久化
        saved = await self._save_token(db, provider_code, owner, tok)
        # 清掉所有者的 state verifier（成功路径）
        return True, "ok", saved

    async def _complete_token_in_code(
        self, provider: OAuthProviderConfig, owner: str, code: str, db: AsyncSession,
    ) -> Tuple[bool, str, Optional[OAuthToken]]:
        """Cline 收尾：先直接解码 code（其本质是 base64 token），失败再走 POST 换票兜底。"""
        tok = decode_cline_code(code)
        if tok is None:
            ep = provider.extra_params or {}
            redirect_uri = self._pending_redirect.get(provider.code) or provider.redirect_uri
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(provider.token_url, json={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_type": ep.get("client_type", "extension"),
                        "redirect_uri": redirect_uri,
                    }, headers={"Content-Type": "application/json", "Accept": "application/json"})
            except Exception as e:
                return False, f"cline exchange request failed: {e}", None
            if resp.status_code >= 400:
                return False, f"cline exchange HTTP {resp.status_code}: {resp.text[:200]}", None
            try:
                body = resp.json()
            except Exception:
                return False, "cline exchange invalid JSON", None
            inner = body.get("data") if isinstance(body.get("data"), dict) else body
            access = inner.get("accessToken") or inner.get("access_token")
            if not access:
                return False, "cline exchange missing accessToken", None
            tok = {
                "access_token": access,
                "refresh_token": inner.get("refreshToken") or inner.get("refresh_token") or "",
                "expires_in": _expires_in_from(
                    inner.get("expiresAt") or inner.get("expires_at"),
                    inner.get("expiresIn") or 3600),
                "token_type": "Bearer",
                "scope": "cline",
            }
        saved = await self._save_token(db, provider.code, owner, tok)
        self._pending_sessions.pop(provider.code, None)
        return True, "ok", saved

    # ── LobsterAI（有道）登录：本地回调 + authCode 换 token ──────────
    async def start_lobsterai_login(self, owner: str = "__default") -> dict:
        """生成登录会话（uuid + first_keyfrom + state）并返回登录 URL。

        ⚠️ portal 的 redirect_uri **必须是 127.0.0.1 形态**（登录页会校验），
        因此远程部署时用户浏览器回调不到 AIGate —— 走「粘贴回调 URL」模式：
        前端打开 login_url，用户在自己机器完成授权，浏览器落到
        `http://127.0.0.1:18090/auth/callback?code=...&state=...`（连接失败页），
        用户把地址栏 URL 整段粘贴回 AIGate 的「完成登录」输入框。
        """
        import uuid as _uuid
        import secrets as _secrets
        from server.core import lobsterai as lb
        state = _secrets.token_hex(16)
        session = {
            "uuid": str(_uuid.uuid4()),
            "first_keyfrom": str(int(time.time() * 1000)),
            "owner": owner,
        }
        self._lobsterai_sessions[state] = session
        client_version = await lb.resolve_client_version()
        # redirect_uri 里的端口固定 18090（回调 URL 只用于用户粘贴，服务端不监听）
        login_url = lb.build_login_url(18090, state)
        return {
            "state": state,
            "login_url": login_url,
            "client_version": client_version,
            "callback_hint": "http://127.0.0.1:18090/auth/callback?code=...&state=...",
        }

    async def complete_lobsterai_login(
        self, code: str, state: str, db: AsyncSession,
        callback_url: str = "",
    ) -> Tuple[bool, str, Optional[OAuthToken]]:
        """用回调收到的 code 换 token 并持久化。

        code / state 可从回调 URL 整段粘贴里解析（用户直接粘贴地址栏最省事）。
        身份字段（uuid/first_keyfrom/latest_keyfrom/client_version）存进 scope
        列 JSON —— 续期时**必须**原样回传，否则上游拒绝（Jet-Hub 的教训）。
        """
        import json as _json
        from urllib.parse import urlparse, parse_qs
        from server.core import lobsterai as lb
        # 允许用户粘贴整段回调 URL
        if callback_url and (not code or not state):
            try:
                qs = parse_qs(urlparse(callback_url).query)
                code = code or (qs.get("code") or [""])[0]
                state = state or (qs.get("state") or [""])[0]
            except Exception:
                pass
        if not code:
            return False, "缺少 code（请粘贴完整回调 URL）", None
        session = self._lobsterai_sessions.pop(state, None) if state else None
        if session is None:
            # state 不在内存（服务重启 / 用户粘贴了旧 URL）：身份字段只能新生成，
            # uuid 变了不影响本次换票，但续期时上游按新身份处理 —— 可接受降级。
            session = {"uuid": str(__import__("uuid").uuid4()),
                       "first_keyfrom": str(int(time.time() * 1000)),
                       "owner": "__default"}
        owner = session.get("owner") or "__default"
        client_version = await lb.resolve_client_version()
        cred, err = await lb.exchange_auth_code(code, session, client_version)
        if not cred:
            return False, err, None
        tok = {
            "access_token": cred["access_token"],
            "refresh_token": cred["refresh_token"],
            "expires_in": cred["expires_in"],
            "token_type": "Bearer",
            # 身份字段随凭据持久化（续期必需）—— 复用 scope 列存 JSON（同 qoder 做法）
            "scope": _json.dumps({
                "lobsterai": True,
                "uuid": cred["uuid"],
                "first_keyfrom": cred["first_keyfrom"],
                "latest_keyfrom": cred["latest_keyfrom"],
                "client_version": cred["client_version"],
                "uid": cred["user_id"],
                "nickname": cred["nickname"],
            }, ensure_ascii=False),
        }
        saved = await self._save_token(db, "lobsterai", owner, tok)
        return True, "ok", saved

    async def _refresh_lobsterai(
        self, db: AsyncSession, provider: OAuthProviderConfig,
        existing: OAuthToken, refresh_plain: str,
    ) -> Tuple[bool, str]:
        """LobsterAI 续期：请求体 = keyfrom 身份载荷 + refreshToken。

        身份字段从 scope 列 JSON 读回（登录时持久化）；缺失时明确报错而不是
        发一个必然失败的请求（对齐 Jet-Hub「丢失即续期失败只能重登」的结论）。
        """
        import json as _json
        from server.core import lobsterai as lb
        meta = {}
        if existing.scope:
            try:
                parsed = _json.loads(existing.scope)
                if isinstance(parsed, dict):
                    meta = parsed
            except ValueError:
                pass
        if not meta.get("uuid") or not meta.get("first_keyfrom"):
            existing.last_error = "缺少 uuid/first_keyfrom（续期必需）—— 请重新登录"
            await db.commit()
            return False, existing.last_error
        cred = {
            "refresh_token": refresh_plain,
            "uuid": meta.get("uuid", ""),
            "first_keyfrom": meta.get("first_keyfrom", ""),
            "latest_keyfrom": meta.get("latest_keyfrom", ""),
            "client_version": meta.get("client_version", ""),
            "user_id": meta.get("uid", ""),
        }
        new_cred, err = await lb.refresh_token(cred)
        if not new_cred:
            existing.last_error = err[:300]
            if _refresh_credential_dead(0, err):
                existing.is_active = False
            await db.commit()
            return False, err
        # 身份字段沿用旧值（latest_keyfrom 刻意不更新）；uid/nickname 保留
        meta.update({
            "uuid": new_cred["uuid"],
            "first_keyfrom": new_cred["first_keyfrom"],
            "latest_keyfrom": new_cred["latest_keyfrom"],
            "client_version": new_cred["client_version"],
        })
        tok = {
            "access_token": new_cred["access_token"],
            "refresh_token": new_cred["refresh_token"],
            "expires_in": new_cred["expires_in"],
            "token_type": "Bearer",
            "scope": _json.dumps(meta, ensure_ascii=False),
        }
        await self._save_token(db, "lobsterai", existing.owner, tok,
                               update_existing=existing)
        return True, "ok"

    # ── Trae（字节）登录：本地回调**直传 token** + ExchangeToken ──────
    async def start_trae_login(self, owner: str = "__default",
                               domain: str = "") -> dict:
        """生成 machine_id / device_id 与登录 URL。

        ⚠️ machine_id / device_id 由客户端生成且**不在任何响应里** → 随凭据
        持久化（存 scope 列 JSON）。AIGate 走「粘贴回调 URL」模式：用户在自己
        机器完成授权后，把地址栏 URL 整段粘回来（回调落在 127.0.0.1，服务端接不到）。
        """
        import secrets as _secrets
        from server.core import trae as tr
        state = _secrets.token_hex(16)
        session = {
            "machine_id": tr.generate_machine_id(),
            "device_id": tr.generate_device_id(),
            "owner": owner,
            "domain": domain,
        }
        self._trae_sessions[state] = session
        port = 18080
        return {
            "state": state,
            "login_url": tr.build_login_url(port, session["machine_id"],
                                            session["device_id"], domain),
            "callback_hint": "http://127.0.0.1:18080/authorize?refreshToken=...&userInfo=...",
        }

    async def complete_trae_login(
        self, callback_url: str, state: str, db: AsyncSession,
        domain: str = "",
    ) -> Tuple[bool, str, Optional[OAuthToken]]:
        """解析回调 URL → ExchangeToken → GetUserInfo → 持久化。

        ⚠️ 回调**直接回传 token**（refreshToken / userInfo / userJwt），没有 ?code=。
        带 code / authCodeInfo 的回调是上游的 PKCE 新流程 —— **未支持**，
        `parse_callback` 会给出精确报错（不是含糊的「缺少 refreshToken」）。
        """
        import json as _json
        from server.core import trae as tr
        info, reason, is_pkce = tr.parse_callback(callback_url or "")
        if info is None:
            return False, reason, None
        if not state:
            state = str(info.get("state") or "")
        session = self._trae_sessions.pop(state, None) if state else None
        if session is None:
            # 服务重启 / 用户粘贴旧 URL：身份字段只能新生成。
            # ⚠️ 必须明确告警：machine_id 变了就是换设备，可能触发风控。
            session = {"machine_id": tr.generate_machine_id(),
                       "device_id": tr.generate_device_id(),
                       "owner": "__default", "domain": domain}
            logger.warning("trae: state=%s 不在内存，machine_id 已重新生成"
                           "（上游可能按新设备处理）", state)
        dom = session.get("domain") or domain
        refresh_plain = str(info.get("refresh_token") or "")
        if not refresh_plain:
            return False, "回调未携带 refreshToken（请确认粘贴的是完整回调 URL）", None
        exchange, err = await tr.exchange_refresh_token(refresh_plain, dom)
        if exchange is None:
            return False, err, None
        uid = str(info.get("uid") or "")
        nickname = str(info.get("nickname") or "")
        # GetUserInfo 补 uid/nickname（失败不阻塞 —— 回调 userInfo 通常已够用）
        try:
            fetched, _ferr = await tr.fetch_user_info(
                str(exchange.get("access_token") or ""), dom)
            if fetched:
                uid = uid or str(fetched.get("uid") or "")
                nickname = nickname or str(fetched.get("screen_name") or "")
        except Exception:
            pass
        cred = tr.build_credential(exchange, {"uid": uid, "nickname": nickname},
                                   session["machine_id"], session["device_id"], dom)
        tok = {
            "access_token": cred["access_token"],
            "refresh_token": cred["refresh_token"],
            "expires_in": cred["expires_in"],
            "token_type": "Bearer",
            "scope": _json.dumps({
                "trae": True,
                "machine_id": cred["machine_id"],
                "device_id": cred["device_id"],
                "uid": cred["uid"],
                "nickname": cred["nickname"],
                "domain": cred["domain"],
            }, ensure_ascii=False),
        }
        saved = await self._save_token(db, "trae", session["owner"], tok)
        return True, "ok", saved

    async def _refresh_trae(
        self, db: AsyncSession, provider: OAuthProviderConfig,
        existing: OAuthToken, refresh_plain: str,
    ) -> Tuple[bool, str]:
        """Trae 续期：ExchangeToken（**轮换** refresh_token，身份字段不动）。"""
        import json as _json
        from server.core import trae as tr
        meta = {}
        if existing.scope:
            try:
                parsed = _json.loads(existing.scope)
                if isinstance(parsed, dict):
                    meta = parsed
            except ValueError:
                pass
        if not meta.get("machine_id") or not meta.get("device_id"):
            existing.last_error = "缺少 machine_id/device_id（续期与请求头必需）—— 请重新登录"
            await db.commit()
            return False, existing.last_error
        dom = str(meta.get("domain") or "")
        exchange, err = await tr.exchange_refresh_token(refresh_plain, dom)
        if exchange is None:
            existing.last_error = err[:300]
            # 终态判定（对齐 Jet-Hub 的三条依据，按 error 文本粗判）
            if any(k in err for k in ("HTTP 401", "HTTP 403", "失效", "invalid")):
                existing.is_active = False
            await db.commit()
            return False, err
        merged = tr.apply_refresh(
            {"access_token": "", "refresh_token": refresh_plain,
             "machine_id": meta.get("machine_id", ""),
             "device_id": meta.get("device_id", ""), "expires_at": ""},
            exchange)
        tok = {
            "access_token": merged["access_token"],
            "refresh_token": merged["refresh_token"],
            "expires_in": merged.get("expires_in") or 3600,
            "token_type": "Bearer",
            # 身份字段永不重新生成；uid/nickname 保留
            "scope": existing.scope or _json.dumps(meta, ensure_ascii=False),
        }
        await self._save_token(db, "trae", existing.owner, tok,
                               update_existing=existing)
        return True, "ok"

    # ── CodeArts（华为）登录：portal OAuth（PKCE + DPoP）──────
    async def start_codearts_login(self, owner: str = "__default") -> dict:
        """起登录会话 → {state, login_url, callback_hint}。

        portal 把回调主机名锁死 127.0.0.1（AIGate 监听不到）→ 走「粘贴回调 URL」。
        端口**必须 ≥10000**（低端口 portal 直接拒绝），用 pick_callback_port()。
        """
        import secrets as _secrets
        from server.core import codearts as ca
        state = _secrets.token_hex(16)
        port = ca.pick_callback_port()
        pkce = ca.generate_pkce_pair()
        kp = ca.generate_dpop_key_pair()
        ticket_id = ca.generate_ticket_id()
        self._codearts_sessions[state] = {
            "port": port,
            "code_verifier": pkce.code_verifier,
            "dpop_jwk": kp["private_key_jwk"],
            "ticket_id": ticket_id,
            "owner": owner,
        }
        return {
            "state": state,
            "login_url": ca.build_oauth_login_url(port, pkce, ticket_id),
            "callback_hint": f"http://127.0.0.1:{port}/oauth/callback?code=...&state=...",
        }

    async def complete_codearts_login(
        self, code: str, state: str, db: AsyncSession,
        callback_url: str = "",
    ) -> Tuple[bool, str, Optional[OAuthToken]]:
        """用回调 code 换凭据并持久化（旧流程 secret 走 ticket 换一次性 AK/SK）。"""
        import json as _json
        from server.core import codearts as ca
        parsed = ca.parse_callback_payload(callback_url or "", code or "", state or "")
        _kind = parsed.get("kind")
        if _kind == "invalid":
            return False, parsed.get("error") or "回调参数无效", None
        _sess_state = parsed.get("state") or state
        session = self._codearts_sessions.pop(_sess_state, None) or {}
        owner = session.get("owner") or "__default"
        if _kind == "oauth":
            cred, err = await ca.exchange_authorization_code(
                parsed.get("code", ""), session.get("code_verifier", ""),
                int(session.get("port") or 0), session.get("dpop_jwk") or {})
        else:   # ticket：旧流程回退，只能换一次性 AK/SK（**无** refresh_token）
            cred, err = await ca.exchange_ticket(
                session.get("ticket_id", ""), parsed.get("secret", ""))
        if cred is None:
            return False, err, None
        # 凭据整体以 JSON 存进 access_token 列 —— 推理侧要 AK/SK/SecurityToken
        # 一起拿去签名，拆字段会引入第二套真相源
        payload = cred.as_dict()
        payload["code_verifier"] = session.get("code_verifier", "")
        payload["dpop_private_key_jwk"] = session.get("dpop_jwk")
        tok = {
            "access_token": _json.dumps(payload, ensure_ascii=False),
            # refresh_token 列**也要写**：`_do_refresh` 以该列非空为前置判据
            # （只塞进 JSON 会被 "no refresh_token stored" 提前挡下）
            "refresh_token": cred.refresh_token or "",
            "expires_in": ca.expires_in_from(cred.expires_at),
            "token_type": "AK/SK",
            "scope": _json.dumps({
                "codearts": True,
                "user_name": cred.user_name,
                "user_id": cred.user_id,
                "domain_id": cred.domain_id,
            }, ensure_ascii=False),
        }
        saved = await self._save_token(db, "codearts", owner, tok)
        return True, "ok", saved

    async def _refresh_codearts(
        self, db: AsyncSession, provider: OAuthProviderConfig,
        existing: OAuthToken, refresh_plain: str,
    ) -> Tuple[bool, str]:
        """CodeArts 续期：AK/SK/SecurityToken 过 DPoP 换一套新的。

        ⚠️ 本方法**只在** refresh_token() 的 Single Flight 内被调用（`_do_refresh`
        由 `refresh_token()` 加锁后调用）—— 这是硬要求：refresh_token 一次性轮换，
        并发刷新会互相作废（STS5.1806），只能让用户重新登录。
        """
        import json as _json
        from server.core import codearts as ca
        try:
            cred = _json.loads(self._crypto.decrypt(existing.access_token_enc))
        except (ValueError, TypeError):
            existing.last_error = "凭据不是 JSON，请重新登录"
            await db.commit()
            return False, existing.last_error
        new_cred, err, dead = await ca.refresh_token(
            cred.get("refresh_token", ""), cred.get("code_verifier", ""),
            cred.get("dpop_private_key_jwk") or {})
        if new_cred is None:
            existing.last_error = err[:300]
            if dead:
                existing.is_active = False
            await db.commit()
            return False, err
        # ⚠️ 新 refresh_token 必须**立即**回写（一次性轮换）；code_verifier /
        # DPoP 私钥 / 身份字段不随响应变化，保留旧值
        payload = new_cred.as_dict()
        payload["code_verifier"] = cred.get("code_verifier", "")
        payload["dpop_private_key_jwk"] = cred.get("dpop_private_key_jwk")
        tok = {
            "access_token": _json.dumps(payload, ensure_ascii=False),
            # 新 refresh_token（一次性轮换）**立即**回写两处：JSON 里一份供
            # exchange_ticket/审计，列里一份供 `_do_refresh` 的前置判据
            "refresh_token": new_cred.refresh_token or refresh_plain,
            "expires_in": ca.expires_in_from(new_cred.expires_at),
            "token_type": "AK/SK",
            "scope": existing.scope,
        }
        await self._save_token(db, "codearts", existing.owner, tok,
                               update_existing=existing)
        return True, "ok"

    # ── Freebuff（Codebuff 免费层）：CLI 授权码轮询登录 ──────
    async def start_freebuff_login(self, owner: str = "__default") -> dict:
        """POST /api/auth/cli/code 拿登录 URL，并启动后台轮询收 authToken。

        与官方 CLI 同协议（免装客户端）：用户在浏览器打开 login_url 用 Google
        登录并授权，后台每 5 秒轮询 /api/auth/cli/status，拿到 user.authToken
        即落库。无需回调地址，故远程部署也能用。
        """
        import secrets as _secrets
        from server.core import freebuff as fb
        state = _secrets.token_hex(16)
        info, err = await fb.start_cli_login(proxy=self._proxy_url())
        if not info:
            return {"error": f"Freebuff 登录启动失败：{err}"}
        self._freebuff_sessions[state] = {
            "info": info, "owner": owner, "created_at": time.time(),
        }
        asyncio.create_task(self._poll_freebuff_login(state, info, owner))
        return {
            "state": state,
            "login_url": info["login_url"],
            "poll_interval_ms": 5000,
            "owner": owner,
        }

    async def _poll_freebuff_login(self, state: str, info: dict, owner: str):
        """轮询等用户完成 Google 登录；拿到 authToken 即落库（5 分钟超时）。"""
        from server.core import freebuff as fb
        deadline = time.time() + 300
        while time.time() < deadline:
            await asyncio.sleep(5)
            user, err, pending = await fb.poll_cli_login(info, proxy=self._proxy_url())
            if user:
                import json as _json
                tok = {
                    "access_token": str(user.get("authToken") or ""),
                    # authToken 长期有效、无标准刷新（refresh_style=none）
                    "refresh_token": "",
                    "expires_in": 365 * 86400,
                    "token_type": "Bearer",
                    # 身份字段（uid/email）存 scope 列 JSON：额度/日志展示要用，
                    # 且与 lobsterai/qoder 的既有做法一致
                    "scope": _json.dumps({
                        "uid": user.get("id"), "email": user.get("email"),
                        "username": user.get("username"),
                    }, ensure_ascii=False),
                }
                try:
                    async with AsyncSessionLocal() as db:
                        # 身份守卫：freebuff 的 authToken 不是 JWT，token_identity()
                        # 取不到 → 走 scope 里的 uid 兜底比对（同一次登录的 uid 不变）。
                        target_owner = owner
                        existing = await self._get_token_record(db, "freebuff", owner)
                        if existing:
                            new_uid = str(user.get("id") or "")
                            old_uid = ""
                            if existing.scope:
                                try:
                                    old_uid = str((json.loads(existing.scope) or {}).get("uid") or "")
                                except (ValueError, TypeError):
                                    old_uid = ""
                            if new_uid and old_uid and new_uid != old_uid:
                                target_owner = await self._next_free_owner(db, "freebuff")
                                logger.warning(
                                    "freebuff: 登录 uid「%s」与连接「%s」（uid「%s」）不同，"
                                    "自动改存为新账号「%s」（不覆盖原凭据）",
                                    new_uid, owner, old_uid, target_owner)
                        await self._save_token(db, "freebuff", target_owner, tok)
                    logger.info("freebuff authToken acquired for %s", target_owner)
                except Exception as e:
                    logger.warning("freebuff token save failed: %s", e)
                finally:
                    self._freebuff_sessions.pop(state, None)
                return
            if not pending:
                logger.info("freebuff login aborted: %s", err)
                self._freebuff_sessions.pop(state, None)
                return
        self._freebuff_sessions.pop(state, None)

    def _proxy_url(self):
        """当前代理池出口（freebuff 免费层按出口国别分层，走代理可换出口）。"""
        try:
            from server.core.proxy_pool import get_proxy_pool
            return get_proxy_pool().proxied_kwargs().get("proxy")
        except Exception:
            return None

    async def complete_pending(
        self, code: str, db: AsyncSession,
    ) -> Tuple[bool, str, Optional[OAuthToken]]:
        """回调未带回 state 时（Cline authorize 不保证回显）按 authorize 时的挂起会话收尾。"""
        for provider_code, packed in list(self._pending_sessions.items()):
            provider = get_oauth_provider(provider_code)
            if not provider or not (provider.extra_params or {}).get("token_in_code"):
                continue
            owner = packed.split("|", 2)[1] if "|" in packed else "__default"
            ok, msg, saved = await self._complete_token_in_code(provider, owner, code, db)
            if ok:
                return True, msg, saved
        return False, "no pending authorize session matches this code", None

    # ── refresh 主动刷新 ──
    async def refresh_token(
        self,
        provider_code: str,
        db: AsyncSession,
        owner: str = "__default",
    ) -> Tuple[bool, str]:
        """
        主动刷新 access_token。
        Single Flight: 同 (provider+owner) 并发刷新合并为 1 个
        如果已有 refresh 进行中，等结果；否则自己执行。
        """
        lock_key = self._key(provider_code, owner)
        if lock_key in self._inflight:
            # P1-14: 必须把 leader 的真实结果回传，不能无条件报成功。
            # 且 wait_for 超时会 cancel 共享 Future（asyncio 语义）→ leader 的
            # set_result 抛 InvalidStateError 穿透到请求路径。用 shield 保护。
            try:
                res = await asyncio.wait_for(asyncio.shield(self._inflight[lock_key]), timeout=15)
                return res
            except asyncio.TimeoutError:
                return False, "previous refresh timed out"
            except Exception as e:
                return False, str(e)

        fut = asyncio.get_event_loop().create_future()
        self._inflight[lock_key] = fut
        try:
            res = await self._do_refresh(db, provider_code, owner)
            # P1-14: waiter 超时可能已 cancel 该 Future → set 前必须判状态
            if not fut.done():
                fut.set_result(res)
            return res
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)
            return False, str(e)
        finally:
            self._inflight.pop(lock_key, None)

    async def _do_refresh(self, db: AsyncSession, provider_code: str, owner: str) -> Tuple[bool, str]:
        provider = get_oauth_provider(provider_code)
        if not provider:
            return False, f"unknown provider {provider_code}"
        existing = await self._get_token_record(db, provider_code, owner)
        # ── 长期凭证（refresh_style=none，如 u1s1 api_key / freebuff authToken）──
        # ⚠️ 必须在 refresh_token 检查**之前**：这类 provider 本就没有 refresh_token，
        # 先检查会得到 "no refresh_token stored" 的假错误（freebuff 实测踩到）。
        if (provider.extra_params or {}).get("refresh_style") == "none":
            if not existing:
                return False, f"no token record for {provider_code}/{owner}"
            return True, "long-lived credential (re-login when it expires)"
        if not existing or not existing.refresh_token_enc:
            return False, "no refresh_token stored"
        refresh_plain = self._crypto.decrypt(existing.refresh_token_enc)
        # ── 非标准刷新协议按 extra_params.refresh_style 分发 ──
        _style = (provider.extra_params or {}).get("refresh_style")
        if _style == "codebuddy" or provider_code == "codebuddy_cn":
            # 腾讯系（CN/国际服同构）：X-Refresh-Token 头 + 空 JSON body
            return await self._refresh_codebuddy(db, provider, existing, refresh_plain)
        if _style == "muse":
            # Meta Muse：refresh_token 列存的是 OAuth access token，「刷新」= 重铸 LLM| key
            return await self._refresh_muse(db, provider, existing, refresh_plain)
        if _style == "cline":
            # Cline：JSON body {refreshToken,grantType,clientType} → {data:{accessToken,expiresAt}}
            return await self._refresh_cline(db, provider, existing, refresh_plain)
        if _style == "lobsterai":
            # LobsterAI：keyfrom 身份载荷 + refreshToken（身份字段从 scope 读回）
            return await self._refresh_lobsterai(db, provider, existing, refresh_plain)
        if _style == "trae":
            # Trae：ExchangeToken 轮换 refreshToken；身份字段从 scope 读回
            return await self._refresh_trae(db, provider, existing, refresh_plain)
        if _style == "codearts":
            # CodeArts：DPoP 换新 AK/SK/SecurityToken（一次性轮换，必须串行 —— 本
            # 调用点已被 refresh_token() 的 Single Flight 包住）
            return await self._refresh_codearts(db, provider, existing, refresh_plain)
        # ── Qoder device_token（CN CLI 语义：device token 就是聊天 token） ──
        # refresh_style=device_token 是显式声明（qoder_cn）；国际版保留旧的
        # device_code_only 开关兼容（历史配置）。
        if _style == "device_token" or (
                provider_code == "qoder" and (provider.extra_params or {}).get("device_code_only")):
            return await self._refresh_device_token(db, provider, existing, refresh_plain)
        refresh_url = provider.refresh_url or provider.token_url
        post_data = {
            "grant_type": "refresh_token",
            "client_id": provider.client_id,
            "refresh_token": refresh_plain,
        }
        if provider.client_secret:
            post_data["client_secret"] = provider.client_secret
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(refresh_url, data=post_data,
                                      headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            err = resp.text[:300]
            # P1-15: 只有「凭证真的失效」才永久下线；5xx/429/网络抖动只记错误，
            # 否则上游一次抖动就把连接判死刑、须人工重登。
            existing.last_error = err
            if _refresh_credential_dead(resp.status_code, err):
                existing.is_active = False
            await db.commit()
            return False, f"refresh endpoint HTTP {resp.status_code}: {err}"
        try:
            tok = resp.json()
        except Exception:
            return False, "invalid JSON from refresh"
        await self._save_token(db, provider_code, owner, tok, update_existing=existing)
        return True, "ok"

    # ── CodeBuddy CN 专属 refresh（X-Refresh-Token 头 + 空 JSON body） ──
    async def _refresh_codebuddy(
        self, db: AsyncSession, provider: OAuthProviderConfig,
        existing: OAuthToken, refresh_plain: str,
    ) -> Tuple[bool, str]:
        ep = provider.extra_params or {}
        ua = ep.get("user_agent", "CLI/2.63.2 CodeBuddy/2.63.2")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": ua,
            "X-Requested-With": "XMLHttpRequest",
            "X-Domain": ep.get("x_domain", "copilot.tencent.com"),
            "X-Refresh-Token": refresh_plain,
            "X-Auth-Refresh-Source": "plugin",
            "X-Product": ep.get("x_product", "SaaS"),
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(provider.refresh_url, headers=headers, content="{}")
            if resp.status_code >= 400:
                err = resp.text[:300]
                # P1-15: 同上——仅凭证失效才下线
                existing.last_error = err
                if _refresh_credential_dead(resp.status_code, err):
                    existing.is_active = False
                await db.commit()
                return False, f"http {resp.status_code}: {err}"
            data = resp.json()
            # 腾讯返回：{ "code": 0, "data": { "accessToken": "...", "refreshToken": "...", "expiresIn": 3600 } }
            if data.get("code") != 0 or not data.get("data", {}).get("accessToken"):
                err = data.get("msg") or "no accessToken in response"
                existing.last_error = err
                # P1-15: 业务码非 0 也需甄别（限流/风控不该下线；refresh token 无效才下线）
                if _refresh_credential_dead(200, f"{data.get('code')} {err}"):
                    existing.is_active = False
                await db.commit()
                return False, f"tencent code={data.get('code')}: {err}"
            tok_inner = data["data"]
            # 适配 _save_token 的标准字段名
            tok = {
                "access_token": tok_inner.get("accessToken"),
                "refresh_token": tok_inner.get("refreshToken") or refresh_plain,
                "expires_in": tok_inner.get("expiresIn") or 3600,
                "token_type": "Bearer",
            }
            await self._save_token(db, provider.code, existing.owner, tok, update_existing=existing)
            return True, "ok"
        except Exception as e:
            return False, f"refresh_codebuddy exception: {e}"

    # ── u1s1（有一说一）设备登录：复刻官方 CLI login.js，免客户端 ──
    async def start_u1s1_device(self, provider_code: str, db: AsyncSession,
                                owner: str = "__default") -> dict:
        """
        1) 生成 EC P-256 密钥对，私钥随设备凭证持久化（u1s1 现强制「客户端信号」=
           RFC9449 DPoP：推理请求须 Authorization: DPoP <device_token> + dpop proof，
           proof 由该私钥逐请求签发；旧的“api_key 不依赖 DPoP”实测结论已被平台收紧）
        2) POST /auth/device/start {public_jwk, device_name, client_version}
        3) 返回 verify_url（用户在浏览器登录并批准）+ 后台轮询 /auth/device/poll
        4) 批准后拿 {api_key: u1s1-…, device_token: u1s1d-…}，api_key 存为 access_token，
           device_token 存 refresh_token，密钥对加密存 device_key_enc
        """
        provider = get_oauth_provider(provider_code)
        if not provider:
            return {"error": f"unknown provider {provider_code}"}
        ep = provider.extra_params or {}
        if ep.get("auth_mode") != "u1s1_device":
            return {"error": "provider not u1s1_device mode"}
        try:
            import base64 as _b64
            from cryptography.hazmat.primitives.asymmetric import ec
            priv = ec.generate_private_key(ec.SECP256R1())
            nums = priv.public_key().public_numbers()

            def _coord(n: int) -> str:
                return _b64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()

            public_jwk = {"kty": "EC", "crv": "P-256",
                          "x": _coord(nums.x), "y": _coord(nums.y),
                          "key_ops": ["verify"], "ext": True}
            # 私钥 JWK（d 同宽 32 字节 b64url）：收票后加密随 token 入库，供 DPoP proof 逐请求签发
            private_jwk = {"kty": "EC", "crv": "P-256",
                           "x": _coord(nums.x), "y": _coord(nums.y),
                           "d": _coord(priv.private_numbers().private_value)}
            body = {"public_jwk": public_jwk,
                    "device_name": ep.get("device_name", "AIGate Gateway"),
                    "client_version": ep.get("client_version", "1.11.2")}
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(ep.get("device_start_url"), json=body,
                                         headers={"content-type": "application/json"})
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = str((resp.json().get("error") or {}).get("message", ""))
                except Exception:
                    detail = resp.text[:200]
                return {"error": f"u1s1 device start http {resp.status_code}: {detail[:200]}"}
            data = resp.json()
            verify_url = str(data.get("verify_url") or "")
            poll_secret = str(data.get("poll_secret") or "")
            if not verify_url.startswith(("http://", "https://")) or not poll_secret:
                return {"error": "u1s1 device start missing verify_url/poll_secret"}
            interval = int(data.get("interval") or 2)
            expires_in = int(data.get("expires_in") or 900)
            asyncio.create_task(self._poll_u1s1_device(
                provider_code, poll_secret, owner, interval, expires_in,
                device_keys={"priv": private_jwk, "pub": {k: v for k, v in public_jwk.items() if k not in ("key_ops",)}}))
            return {"state": poll_secret, "login_url": verify_url,
                    "poll_interval_ms": max(1, interval) * 1000, "owner": owner}
        except Exception as e:
            return {"error": f"start_u1s1_device exception: {e}"}

    async def _poll_u1s1_device(self, provider_code: str, poll_secret: str,
                                owner: str, interval: int, expires_in: int,
                                device_keys: Optional[dict] = None):
        """轮询等浏览器批准（官方 CLI 同协议）；status ok 时收 api_key 落库。"""
        provider = get_oauth_provider(provider_code)
        if not provider:
            return
        ep = provider.extra_params or {}
        poll_url = ep.get("device_poll_url")
        deadline = time.time() + max(60, expires_in + 30)
        while time.time() < deadline:
            await asyncio.sleep(max(1, interval))
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(poll_url, json={"poll_secret": poll_secret},
                                             headers={"content-type": "application/json"})
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                if data.get("status") == "expired":
                    logger.info("u1s1 device login expired (user did not approve)")
                    return
                api_key = str(data.get("api_key") or "")
                if data.get("status") == "ok" and api_key.startswith("u1s1-"):
                    async with AsyncSessionLocal() as db:
                        row = await self._save_token(db, provider_code, owner, {
                            "access_token": api_key,
                            "refresh_token": str(data.get("device_token") or ""),
                            # api_key 长期有效（实测无标准刷新）；到期/失效重新登录即可
                            "expires_in": 30 * 86400,
                            "token_type": "Bearer",
                            "scope": "u1s1",
                        })
                        if device_keys and device_keys.get("priv"):
                            import json as _json
                            row.device_key_enc = self._crypto.encrypt(_json.dumps(device_keys))
                            await db.commit()
                            logger.info("u1s1 device DPoP key persisted")
                    logger.info("u1s1 api_key acquired for %s/%s", provider_code, owner)
                    return
            except Exception as e:
                logger.debug("u1s1 poll failed: %s", e)
                continue

    async def get_device_signing_material(self, provider_code: str, db: AsyncSession,
                                          owner: str = "__default") -> Optional[dict]:
        """u1s1 DPoP 签名材料：{bearer, priv_jwk, pub_jwk}；未随设备密钥登录过则 None。"""
        try:
            row = await self._get_token_record(db, provider_code, owner)
            if (not row or not row.is_active) and owner == "__default":
                # 与 pick_access_token 同款兜底：主账号改名/停用后仍能取到签名材料
                row = await self._get_any_active_token_record(db, provider_code)
            if not row or not row.device_key_enc or not row.refresh_token_enc:
                return None
            import json as _json
            keys = _json.loads(self._crypto.decrypt(row.device_key_enc))
            bearer = self._crypto.decrypt(row.refresh_token_enc)
            if not bearer.startswith("u1s1d-") or not keys.get("priv"):
                return None
            return {"bearer": bearer, "priv": keys["priv"], "pub": keys.get("pub")}
        except Exception:
            return None

    # ── Meta Muse Code 设备码流（RFC 8628 → 铸 LLM| key） ──
    # 见 oauth_registry "muse" 条目的协议说明。此处实现三个动作：
    #   start（要设备码）→ 轮询换 OAuth token → 铸 key → 落库。
    # 前置/铸 key 端点按国别封锁（服务端须经代理），故统一走代理池 force。
    async def start_muse_device(self, provider_code: str, db: AsyncSession,
                                owner: str = "__default") -> dict:
        """1) POST 设备授权端点（form client_id）拿 user_code/verification_uri
        2) 返回 login_url（优先 verification_uri_complete，已内嵌 code）给前端
        3) 后台轮询 OAuth token → 铸 LLM| key → 落库"""
        provider = get_oauth_provider(provider_code)
        if not provider:
            return {"error": f"unknown provider {provider_code}"}
        ep = provider.extra_params or {}
        if ep.get("auth_mode") != "muse_device":
            return {"error": "provider not muse_device mode"}
        device_url = ep.get("device_url") or provider.authorize_url
        # 整条设备流固定同一个代理出口（2026-10-18）：设备码由服务器经代理申请、
        # 由用户浏览器批准；中途换出口会让 Meta 侧看到不同来源（CLIProxyAPI #6242
        # 的地区类拒绝）。这里只取一次 proxy_kwargs，轮询与铸 key 全部复用。
        proxy_kwargs = _force_proxy_kwargs()
        # 传输层失败重试 2 次：Meta 走 mihomo 出口，SSL EOF 抖动实测常见
        # （2026-10-16 生产：用户点 5 次连接，3 次成功 2 次撞上出口抖动被记成 400）。
        # HTTP 4xx 是上游判定（如设备码申请限流），不重试。
        resp = None
        last_exc = ""
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=25, **proxy_kwargs) as client:
                    resp = await client.post(
                        device_url,
                        data={"client_id": provider.client_id},
                        headers={"Accept": "application/json",
                                 "User-Agent": MUSE_USER_AGENT,
                                 "x-api-version": ep.get("api_version", "1.0.0")},
                    )
                break
            except Exception as e:
                last_exc = f"start_muse_device exception: {e}"
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
        if resp is None:
            return {"error": last_exc or "start_muse_device failed"}
        if resp.status_code >= 400:
            return {"error": f"muse device start http {resp.status_code}: {resp.text[:200]}"}
        try:
            data = resp.json()
        except Exception as e:
            return {"error": f"muse device start json: {e}"}
        device_code = str(data.get("device_code") or "")
        user_code = str(data.get("user_code") or "")
        verification_uri = str(data.get("verification_uri") or "")
        complete = str(data.get("verification_uri_complete") or "")
        if not device_code or not user_code or not verification_uri.startswith(("http://", "https://")):
            return {"error": "muse device authorization response is missing fields"}
        interval = int(data.get("interval") or 5)
        expires_in = int(data.get("expires_in") or 600)
        record_device_flow(provider_code, device_code, "pending", "")
        asyncio.create_task(self._poll_muse_device(
            provider_code, device_code, owner, interval, expires_in,
            proxy_kwargs=proxy_kwargs))
        return {"state": device_code, "login_url": complete or verification_uri,
                "user_code": user_code,
                "code_embedded": bool(complete),
                "poll_interval_ms": max(1, interval) * 1000, "owner": owner}

    async def _poll_muse_device(self, provider_code: str, device_code: str,
                                owner: str, interval: int, expires_in: int,
                                proxy_kwargs: dict = None):
        """RFC 8628 轮询：authorization_pending=继续，slow_down=间隔 +5s，
        其它错误=终止；拿到 access_token 后铸 key 落库。
        proxy_kwargs 由 start_muse_device 传入 → 整条流固定同一出口。"""
        provider = get_oauth_provider(provider_code)
        if not provider:
            return
        ep = provider.extra_params or {}
        if proxy_kwargs is None:
            proxy_kwargs = _force_proxy_kwargs()
        token_url = ep.get("device_token_url") or provider.token_url
        interval = max(1, int(interval or 5))
        deadline = time.time() + max(60, expires_in + 30)
        slow_downs = 0
        while time.time() < deadline:
            await asyncio.sleep(interval)
            try:
                async with httpx.AsyncClient(timeout=20, **proxy_kwargs) as client:
                    resp = await client.post(
                        token_url,
                        data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                              "client_id": provider.client_id,
                              "device_code": device_code},
                        headers={"Accept": "application/json",
                                 "User-Agent": MUSE_USER_AGENT,
                                 "x-api-version": ep.get("api_version", "1.0.0")},
                    )
                try:
                    body = resp.json() if resp.status_code < 500 else None
                except Exception:
                    body = None
                if body is None and resp.status_code >= 400:
                    # 非 JSON 错误体（WAF/网关拦截页，9router-go PR #302 实证 Meta 会这么干）：
                    # 当成终止，别按 pending 空转到过期；429/5xx 属瞬时，继续轮询
                    if resp.status_code in (429,) or resp.status_code >= 500:
                        continue
                    logger.warning("muse poll terminal: HTTP %s non-JSON body", resp.status_code)
                    record_device_flow(provider_code, device_code, "failed",
                                       f"上游返回非 JSON 错误体（HTTP {resp.status_code}，疑似 WAF/网关拦截），已终止")
                    return
                if body and not body.get("error"):
                    access = str(body.get("access_token") or "")
                    if not access:
                        logger.warning("muse poll: token response has no access_token")
                        record_device_flow(provider_code, device_code, "failed",
                                           "token 响应缺少 access_token（上游异常响应）")
                        return
                    await self._finish_muse_login(provider_code, owner, access, ep, body,
                                                  device_code=device_code,
                                                  proxy_kwargs=proxy_kwargs)
                    return
                err = (body or {}).get("error")
                if err == "slow_down":
                    slow_downs += 1
                    interval += 5
                elif err not in (None, "authorization_pending"):
                    logger.warning("muse device login failed: %s", err or resp.status_code)
                    _hint = ""
                    if err == "expired_token":
                        _hint = ("——设备码 10 分钟内未被批准。若浏览器侧曾报 "
                                 "「Sign-in failed / Token exchange failed」，是 Meta 门户在浏览器侧"
                                 "拒绝了本次授权（CLIProxyAPI #6242 同判：地区限制 / 企业托管账号 / "
                                 "代理扩展干扰；设备码由服务器经代理申请、浏览器在另一网络也会触发）。"
                                 "可改用本机同出口登录：python scripts/muse_login.py --proxy <代理> "
                                 "铸出 LLM| key 后在「导入 Token」粘贴")
                    record_device_flow(provider_code, device_code, "failed",
                                       f"设备授权失败：{err or resp.status_code} {_hint}")
                    return
            except Exception as e:
                logger.debug("muse poll failed: %s", e)
                continue
        logger.info("muse device login timed out (%d slow_downs)", slow_downs)
        record_device_flow(provider_code, device_code, "failed",
                           f"设备流超时（{slow_downs} 次 slow_down）：浏览器侧未在有效期内完成批准。"
                           "若浏览器报「Sign-in failed / Token exchange failed」，是 Meta 门户在浏览器侧"
                           "拒绝（地区/出口/账号类型）。备选：本机跑 python scripts/muse_login.py "
                           "--proxy <代理>，把铸出的 LLM| key 用「导入 Token」录入")

    async def _finish_muse_login(self, provider_code: str, owner: str, oauth_access: str,
                                 ep: dict, token_body: dict = None, device_code: str = "",
                                 proxy_kwargs: dict = None):
        """铸 LLM| key 并落库（access_token=key，refresh_token=OAuth token）。"""
        try:
            minted = await self._mint_muse_key(oauth_access, ep, proxy_kwargs=proxy_kwargs)
        except Exception as e:
            logger.warning("muse key mint failed for %s/%s: %s", provider_code, owner, e)
            record_device_flow(provider_code, device_code, "failed",
                               f"浏览器批准成功，但铸造推理 key 失败：{str(e)[:300]}")
            return
        api_key = minted["api_key"]
        account_id = minted.get("account_id") or ""
        email = minted.get("email") or ""
        # OAuth token 存 refresh_token 列，供重铸；其自身寿命未知（上游不保证），
        # 只作为「重铸材料」，不作为推理凭证。
        oauth_expires = int((token_body or {}).get("expires_in") or 0)
        scope_meta = json.dumps({"account_id": account_id, "email": email}, ensure_ascii=False)
        async with AsyncSessionLocal() as db:
            target_owner = owner
            # 新连接若还是自动名，改用账号邮箱（同 CodeBuddy 的做法，仅限自动名）
            if email and _is_auto_allocated_owner(target_owner):
                existing_here = await self._get_token_record(db, provider_code, target_owner)
                if existing_here is None:
                    clash = await self._get_token_record(db, provider_code, email)
                    if clash is None:
                        logger.info("muse: 新连接默认名「%s」→「%s」（账号标识）",
                                    target_owner, email)
                        target_owner = email
            try:
                await self._save_token(db, provider_code, target_owner, {
                    "access_token": api_key,
                    "refresh_token": oauth_access,
                    # 铸出的 LLM| key 是账号绑定的静态凭证（9router/opencode 都不自动重铸）。
                    # 设 30 天寿命 + registry 的 7 天 lead → 每 23 天才重铸一次，
                    # 避免把激进限流的 mint 端点打成 429。
                    "expires_in": 30 * 86400,
                    "refresh_token_expires_in": oauth_expires,
                    "token_type": "Bearer",
                    "scope": scope_meta,
                })
            except Exception as e:
                logger.warning("muse save token failed: %s", e)
                record_device_flow(provider_code, device_code, "failed",
                                   f"key 已铸造但保存连接失败：{str(e)[:200]}")
                return
        logger.info("muse key acquired for %s/%s (account=%s)", provider_code, target_owner, account_id or "?")
        record_device_flow(provider_code, device_code, "ok", "")

    async def _mint_muse_key(self, oauth_access_token: str, ep: dict,
                             proxy_kwargs: dict = None) -> dict:
        """POST api.meta.ai/muse-code/key（JSON {"onboard":true}）→ 解析 LLM| key。

        与官方插件同判据：is_subs_active=False → 订阅未激活；
        无 api_key 且 require_payment/action_url → 需付费；否则视为响应异常。

        429/5xx 重试（照搬 9router：铸 key 端点激进限流，设备码是一次性的，
        mint 失败若直接丢弃 OAuth token，用户必须重新走浏览器批准）。"""
        key_url = ep.get("key_url") or "https://api.meta.ai/muse-code/key"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": MUSE_USER_AGENT,
            "x-api-version": ep.get("api_version", "1.0.0"),
            "Authorization": f"Bearer {oauth_access_token}",
        }
        if proxy_kwargs is None:
            proxy_kwargs = _force_proxy_kwargs()
        resp = None
        for attempt in range(3):
            async with httpx.AsyncClient(timeout=25, **proxy_kwargs) as client:
                resp = await client.post(key_url, json={"onboard": True}, headers=headers)
            if resp.status_code != 429 and resp.status_code < 500:
                break
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
        if resp.status_code >= 400:
            # Meta 错误信封（如 403 code 4705002）带 title/detail/action_url：
            # 解析出来给用户可执行的修复入口，别只丢原始 JSON。
            msg = f"HTTP {resp.status_code} {resp.text[:200]}"
            try:
                err = resp.json()
                if err.get("title") or err.get("detail"):
                    msg = f"HTTP {resp.status_code} " + ": ".join(
                        str(x) for x in (err.get("title"), err.get("detail")) if x)
                    if err.get("action_url"):
                        msg += f" — {err['action_url']}"
            except Exception:
                pass
            raise RuntimeError(f"muse key exchange failed: {msg}")
        data = resp.json()
        if data.get("is_subs_active") is False:
            raise RuntimeError("Muse Code subscription is inactive for this account")
        api_key = str(data.get("api_key") or "").strip()
        if not api_key:
            action = str(data.get("action_url") or data.get("require_payment_action_url") or "").strip()
            if data.get("require_payment") is True or action:
                raise RuntimeError("Muse Code subscription required"
                                   + (f": {action}" if action else ""))
            raise RuntimeError("muse key response is missing api_key")
        return {
            "api_key": api_key,
            "account_id": str(data.get("user_id") or "").strip(),
            "email": str(data.get("user_email") or "").strip().lower(),
        }

    async def _refresh_muse(self, db: AsyncSession, provider: OAuthProviderConfig,
                            existing: OAuthToken, oauth_token: str) -> Tuple[bool, str]:
        """muse 的「刷新」= 用存下的 OAuth token 重铸 LLM| key。

        ⚠️ 失败语义与别的上游不同：**推理凭证（现有 LLM| key）可能仍然有效**，
        失去的只是「重铸能力」。因此永不因重铸失败下线连接，只记 last_error；
        并把 expires_at 往后推（永久失败 30 天 / 瞬时失败 30 分钟）—— 否则刷新
        调度器每 60s 会重试一次，而铸 key 端点激进限流（429）。
        """
        ep = provider.extra_params or {}
        now = _now_utc()
        try:
            minted = await self._mint_muse_key(oauth_token, ep)
        except Exception as e:
            msg = str(e)[:300]
            code = status_from_error_text(msg)
            existing.last_error = (
                "重铸 key 失败（当前推理 key 仍然有效）：" + msg
                + ("" if code != 401 else " —— OAuth 授权已失效，需重新设备码登录"))
            if code in (401, 403):
                existing.expires_at = now + timedelta(days=30)      # 授权失效：停止自动重试
            else:
                existing.expires_at = now + timedelta(minutes=30)   # 瞬时（429/5xx）：半小时后再试
            await db.commit()
            return False, msg
        email = minted.get("email") or ""
        scope_meta = json.dumps({"account_id": minted.get("account_id") or "", "email": email},
                                ensure_ascii=False)
        await self._save_token(db, provider.code, existing.owner, {
            "access_token": minted["api_key"],
            "refresh_token": oauth_token,
            "expires_in": 30 * 86400,
            "token_type": "Bearer",
            "scope": scope_meta,
        }, update_existing=existing)
        return True, "ok"

    # ── Qoder 设备流（本地 PKCE+nonce，轮询 deviceToken/poll 收 dt- token） ──
    async def start_qoder_device(self, provider_code: str, db: AsyncSession,
                                 owner: str = "__default") -> dict:
        """Qoder 设备流（国际版 qoder.com / 国内版 qoder.cn 同协议）。

        1) 本地生成 PKCE(S256) verifier/challenge + nonce + machine_id
        2) 用户浏览器打开 {site}/device/selectAccounts?challenge&nonce&machine_id
           （CN 额外带国内 CLI 的 client_id，见注册表 extra_params/`client_id`）
        3) 后台轮询 {openapi}/api/v1/deviceToken/poll（202/404=pending）
        4) 批准后收 {token: dt-…, user_id, expires_at} → userinfo 补邮箱 → 落库
           （scope 列存 COSY 签名所需 JSON 元数据）
        """
        provider = get_oauth_provider(provider_code)
        if not provider:
            return {"error": f"unknown provider {provider_code}"}
        ep = provider.extra_params or {}
        if ep.get("auth_mode") != "qoder_device":
            return {"error": "provider not qoder_device mode"}
        try:
            import uuid as _uuid
            verifier = secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            ).rstrip(b"=").decode()
            nonce = str(_uuid.uuid4())
            machine_id = str(_uuid.uuid4())
            login_url = (
                f"{ep.get('login_url')}?challenge={challenge}"
                f"&challenge_method=S256&machine_id={machine_id}&nonce={nonce}"
            )
            # CN 登录页要求 client_id（国内 CLI 生产值）；国际版现行流程不带，保持原样
            if provider.client_id:
                login_url += f"&client_id={provider.client_id}"
            asyncio.create_task(self._poll_qoder_device(
                provider_code, nonce, verifier, machine_id, owner))
            return {"state": nonce, "login_url": login_url,
                    "poll_interval_ms": 2000, "owner": owner,
                    "site": ep.get("site") or "qoder"}
        except Exception as e:
            return {"error": f"start_qoder_device exception: {e}"}

    async def _poll_qoder_device(self, provider_code: str, nonce: str,
                                 verifier: str, machine_id: str, owner: str):
        provider = get_oauth_provider(provider_code)
        if not provider:
            return
        ep = provider.extra_params or {}
        poll_url = provider.token_url
        headers = {"Accept": "application/json", "User-Agent": "Go-http-client/2.0"}
        deadline = time.time() + 5 * 60
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(
                        f"{poll_url}?nonce={nonce}&verifier={verifier}&challenge_method=S256",
                        headers=headers)
                if resp.status_code in (202, 404):
                    continue  # 用户还没批
                if resp.status_code >= 400:
                    logger.warning("qoder device poll http %s: %s",
                                   resp.status_code, resp.text[:160])
                    continue
                data = resp.json()
                access = str(data.get("token") or "")
                if not access:
                    continue
                # 到期时间：expires_at（ms/RFC3339）兜底 30 天
                expire_ts = self._qoder_parse_expiry(data.get("expires_at"), data.get("expires_in"))
                expires_in = max(3600, int((expire_ts - time.time())))
                # 补账号信息（尽力而为）
                meta = {"uid": str(data.get("user_id") or ""),
                        "machine_id": machine_id, "email": "", "name": "", "org": ""}
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        ur = await client.get(ep.get("userinfo_url"), headers={
                            "Authorization": f"Bearer {access}",
                            "Accept": "application/json", "User-Agent": "Go-http-client/2.0"})
                    if ur.is_success:
                        ud = ur.json() or {}
                        meta["email"] = str(ud.get("email") or "").strip()
                        meta["name"] = str(ud.get("name") or ud.get("username") or "").strip()
                        meta["org"] = str(ud.get("organization_id") or "").strip()
                        meta["uid"] = meta["uid"] or str(ud.get("id") or ud.get("user_id") or "")
                except Exception:
                    pass
                import json as _json
                async with AsyncSessionLocal() as db:
                    await self._save_token(db, provider_code, owner, {
                        "access_token": access,
                        "refresh_token": str(data.get("refresh_token") or ""),
                        "expires_in": expires_in,
                        "token_type": "Bearer",
                        "scope": _json.dumps(meta, ensure_ascii=False)[:480],
                    })
                logger.info("qoder device token acquired for %s/%s (uid=%s)",
                            provider_code, owner, meta["uid"])
                return
            except Exception as e:
                logger.debug("qoder poll failed: %s", e)
                continue
        logger.info("qoder device flow timed out (user did not approve)")

    @staticmethod
    def _qoder_parse_expiry(expires_at, expires_in) -> float:
        """upstream 到期字段 → unix 秒（ms/秒/RFC3339 容错），兜底 now+30d。"""
        v = expires_at
        if isinstance(v, str) and v.strip().isdigit():
            v = int(v.strip())
        if isinstance(v, (int, float)) and v > 0:
            return float(v) / 1000.0 if v > 1e12 else float(v)
        if isinstance(v, str) and v.strip():
            try:
                dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
                return dt.timestamp()
            except ValueError:
                pass
        if isinstance(expires_in, (int, float)) and expires_in >= 0:
            return time.time() + float(expires_in)
        return time.time() + 30 * 86400

    # ── Cline 专属 refresh（JSON body + data 包裹响应） ──
    async def _refresh_cline(
        self, db: AsyncSession, provider: OAuthProviderConfig,
        existing: OAuthToken, refresh_plain: str,
    ) -> Tuple[bool, str]:
        ep = provider.extra_params or {}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(provider.refresh_url, json={
                    "refreshToken": refresh_plain,
                    "grantType": "refresh_token",
                    "clientType": ep.get("client_type", "extension"),
                }, headers={"Content-Type": "application/json", "Accept": "application/json"})
            if resp.status_code >= 400:
                err = resp.text[:300]
                # P1-15: 仅凭证失效才下线（429/5xx 抖动保留下次重试机会）
                existing.last_error = err
                if _refresh_credential_dead(resp.status_code, err):
                    existing.is_active = False
                await db.commit()
                return False, f"http {resp.status_code}: {err}"
            body = resp.json()
            inner = body.get("data") if isinstance(body.get("data"), dict) else body
            access = inner.get("accessToken")
            if not access:
                err = body.get("message") or body.get("error") or "no accessToken in response"
                existing.last_error = str(err)
                if _refresh_credential_dead(200, str(err)):
                    existing.is_active = False
                await db.commit()
                return False, f"cline refresh: {err}"
            tok = {
                "access_token": access,
                "refresh_token": inner.get("refreshToken") or refresh_plain,
                "expires_in": _expires_in_from(
                    inner.get("expiresAt"), inner.get("expiresIn") or 3600),
                "token_type": "Bearer",
            }
            await self._save_token(db, provider.code, existing.owner, tok, update_existing=existing)
            return True, "ok"
        except Exception as e:
            return False, f"refresh_cline exception: {e}"

    # ── Qoder device token 续期（CN CLI 用；国际版 device_code_only 路径复用） ──
    async def _refresh_device_token(
        self, db: AsyncSession, provider: OAuthProviderConfig,
        existing: OAuthToken, refresh_plain: str,
    ) -> Tuple[bool, str]:
        """POST {openapi}/api/v1/deviceToken/refresh {refresh_token} → 新 dt- 对。

        ⚠️ refresh token **一次性轮换**：上游每次刷新都返回新的 refresh_token，
        必须立刻落库（下次用旧的必 401）。失败分级照 P1-15：仅 401/403 判死，
        5xx/429 只记 last_error 保留重试机会。
        来源：@magpie-community/opencode-qoder-auth 的 refreshDevice()。
        """
        ep = provider.extra_params or {}
        site = str(ep.get("site") or "qoder")
        from server.core.qoder_sign import qoder_openapi_base
        url = f"{qoder_openapi_base(site)}/api/v1/deviceToken/refresh"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json={"refresh_token": refresh_plain},
                                         headers={"Content-Type": "application/json",
                                                  "Accept": "application/json"})
            if resp.status_code >= 400:
                err = resp.text[:300]
                existing.last_error = err
                if _refresh_credential_dead(resp.status_code, err):
                    existing.is_active = False
                await db.commit()
                return False, f"device token refresh HTTP {resp.status_code}: {err}"
            body = resp.json() or {}
            access = str(body.get("token") or body.get("device_token") or "")
            new_refresh = str(body.get("refresh_token") or "")
            if not access:
                existing.last_error = "device token refresh: empty token"
                await db.commit()
                return False, "device token refresh: empty token in response"
            tok = {
                "access_token": access,
                "refresh_token": new_refresh or refresh_plain,
                "expires_in": max(3600, int(
                    self._qoder_parse_expiry(body.get("expires_at"),
                                             body.get("expires_in")) - time.time())),
                "token_type": "Bearer",
                # scope 保留签名元数据（uid/machine_id/email/name）——丢了推理必失败
                "scope": existing.scope or "",
            }
            await self._save_token(db, provider.code, existing.owner, tok,
                                   update_existing=existing)
            return True, "ok"
        except Exception as e:
            return False, f"refresh_device_token exception: {e}"

    # ── CodeBuddy CN 专属 device_poll 流程：首次登录 ──
    async def start_device_poll(
        self,
        provider_code: str,
        db: AsyncSession,
        owner: str = "__default",
    ) -> dict:
        """
        CodeBuddy CN / 国际服 device poll（协议对齐 9router）：
        1) POST {state_url}?platform={platform} body={} → {code:0, data:{state, authUrl}}
        2) 返回 state + login_url（authUrl）给前端弹窗
        3) 同时启动后台轮询，每 poll_interval_ms 拿 token_url?state=xxx
        4) 拿到 token 后持久化
        返回 {"state": "...", "login_url": "https://...authUrl...", "poll_interval_ms": ...,
              "owner": "..."}
        """
        provider = get_oauth_provider(provider_code)
        if not provider:
            return {"error": f"unknown provider {provider_code}"}
        ep = provider.extra_params or {}
        if ep.get("auth_mode") != "device_poll":
            return {"error": "provider not device_poll mode"}
        state_url = ep.get("state_url") or provider.token_url
        ua = ep.get("user_agent", "CLI/2.63.2 CodeBuddy/2.63.2")
        platform = ep.get("platform", "CLI")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": ua,
            "X-Requested-With": "XMLHttpRequest",
            "X-Domain": ep.get("x_domain", "copilot.tencent.com"),
            "X-No-Authorization": "true",
            "X-No-User-Id": "true",
            "X-Product": ep.get("x_product", "SaaS"),
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{state_url}?platform={platform}",
                                         headers=headers, content="{}")
            if resp.status_code >= 400:
                return {"error": f"state URL HTTP {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()
            if data.get("code") != 0 or not (data.get("data") or {}).get("state"):
                return {"error": f"state error: {data.get('msg') or 'missing state/authUrl'}"}
            inner = data["data"]
            state = inner["state"]
            # authUrl = 官方协议字段（9router 实测）；loginUrl 兜底兼容
            login_url = inner.get("authUrl") or inner.get("loginUrl") or \
                        (state_url + ("?state=" + state if state else ""))
            # 启动后台轮询任务（不 awaited，独立协程）
            poll_interval_ms = ep.get("poll_interval_ms", 5000)
            # 设备流状态置 pending：否则前端会读到上一次流程的 failed 终态，
            # 把本次刚开始的登录误报成失败（CodeBuddy 是唯一走本函数的 device_poll）。
            record_device_flow(provider_code, state, "pending", "")
            asyncio.create_task(self._poll_codebuddy_token(
                provider_code, state, owner, poll_interval_ms,
            ))
            return {
                "state": state,
                "login_url": login_url,
                "poll_interval_ms": poll_interval_ms,
                "owner": owner,
            }
        except Exception as e:
            return {"error": f"start_device_poll exception: {e}"}

    async def _poll_codebuddy_token(
        self,
        provider_code: str,
        state: str,
        owner: str,
        poll_interval_ms: int,
        max_attempts: int = 120,
    ):
        """
        每 poll_interval_ms 轮询 token_url?state=xxx，最多 max_attempts 次（默认 ~10 分钟）。
        腾讯协议：code 0 带 accessToken=成功；code 11217=等待用户登录（pending）；
        其余 code 视为本次失败继续重试（可能瞬时）。拿到 token 后持久化到 DB。
        """
        provider = get_oauth_provider(provider_code)
        if not provider:
            return
        ep = provider.extra_params or {}
        ua = ep.get("user_agent", "CLI/2.63.2 CodeBuddy/2.63.2")
        headers = {
            "Accept": "application/json",
            "User-Agent": ua,
            "X-Requested-With": "XMLHttpRequest",
            "X-Domain": ep.get("x_domain", "copilot.tencent.com"),
            "X-No-Authorization": "true",
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
            "X-No-Department-Info": "true",
            "X-Product": ep.get("x_product", "SaaS"),
        }
        token_url = provider.token_url
        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval_ms / 1000.0)
            try:
                async with AsyncSessionLocal() as db:
                    async with httpx.AsyncClient(timeout=15) as client:
                        resp = await client.get(token_url, headers=headers,
                                                 params={"state": state})
                    if resp.status_code >= 400:
                        # 状态码错误，继续等
                        continue
                    data = resp.json()
                    if data.get("code") != 0:
                        continue  # 11217 pending 或其他瞬时错误，继续轮询
                    inner = data.get("data") or {}
                    access = inner.get("accessToken")
                    if not access:
                        # 还在等待用户登录，继续轮询
                        continue
                    # 拿到 token — 适配 _save_token 标准字段
                    tok = {
                        "access_token": access,
                        "refresh_token": inner.get("refreshToken", ""),
                        "expires_in": inner.get("expiresIn") or 3600,
                        "token_type": inner.get("tokenType") or "Bearer",
                        "scope": "codebuddy",
                    }
                    # 身份归一（2026-10-13 用户要求 + 2026-09-22 事故守卫）：
                    # ① 该账号已有连接（可能被改过名）→ 写回那条，防重复连接；
                    # ② 目标槽位属于**另一个账号** → 挪到新槽位，绝不覆盖原凭据；
                    # ③ 全新连接且名字还是自动名（__default/account-N）→ 默认名改用
                    #    账号邮箱/手机号（用户要求）；已有名字（含手动改名）绝不自动改。
                    new_id = token_identity(access)
                    target_owner = owner
                    if new_id:
                        _matched = await self._find_owner_by_identity(db, provider_code, new_id)
                        if _matched:
                            target_owner = _matched
                    if target_owner == owner:
                        existing = await self._get_token_record(db, provider_code, owner)
                        if existing:
                            old_id = token_identity(self._crypto.decrypt(existing.access_token_enc))
                            if new_id and old_id and new_id != old_id:
                                target_owner = await self._next_free_owner(db, provider_code)
                                logger.warning(
                                    "codebuddy %s: 登录账号「%s」与连接「%s」（属于「%s」）不同，"
                                    "自动改存为新账号「%s」（不覆盖原凭据）",
                                    provider_code, new_id, owner, old_id, target_owner)
                    if _is_auto_allocated_owner(target_owner):
                        _row_here = await self._get_token_record(db, provider_code, target_owner)
                        if _row_here is None:
                            _email_name = default_owner_name(access)
                            if _email_name and _email_name != target_owner:
                                _clash = await self._get_token_record(db, provider_code, _email_name)
                                if _clash is None:
                                    logger.info(
                                        "codebuddy %s: 新连接默认名「%s」→「%s」（账号标识）",
                                        provider_code, target_owner, _email_name)
                                    target_owner = _email_name
                    await self._save_token(db, provider_code, target_owner, tok)
                    logger.info("codebuddy %s token acquired after %d polls (owner=%s)",
                                  provider_code, attempt + 1, target_owner)
                    # 终态回写（与 muse 对齐）：同账号重登只换 token、连接数不增，
                    # 前端 watchDeviceFlow 靠「连接数变多」判定成功 → 会一直空等
                    # 到 6 分钟超时并误报「未检测到新连接」。这里把结果写进
                    # _DEVICE_FLOW_STATUS，前端立即提示「已更新原连接」。
                    if target_owner == owner:
                        _msg = (f"登录成功：该账号已有连接「{target_owner}」，"
                                f"token 已更新（连接数不变，属正常）")
                    else:
                        _msg = f"登录成功：已新增/写回连接「{target_owner}」"
                    record_device_flow(provider_code, state, "ok", "", detail=_msg)
                    return
            except Exception as e:
                logger.warning("codebuddy poll attempt %d failed: %s", attempt + 1, e)
                continue
        logger.warning("codebuddy poll for state %s reached max_attempts without token",
                        state)
        # 超时也回写终态：前端 6 分钟到点前就能看到「浏览器未完成登录」，
        # 而不是笼统的「未检测到新连接」。CodeBuddy 的 state 有效期约 10 分钟，
        # 与轮询窗口（120 × 5s）基本同长。
        record_device_flow(
            provider_code, state, "failed",
            "设备流超时：浏览器侧未在有效期内完成登录（state 未被批准）。"
            "请确认登录窗口已打开并完成账号登录/授权；若已有连接且是同一账号，"
            "重登只会更新 token 不会新增连接——如需再加一个账号，请用「强制新增」"
            "并在新窗口登录另一个账号。")

    async def pick_access_token(
        self,
        provider_code: str,
        db: AsyncSession,
        owner: str = "__default",
    ) -> Optional[str]:
        """供 v1_router 使用：返回有效 access_token；必要时主动刷新。

        owner=__default（自动模式）时按多账号自动选号（见 pick_access_token_ex），
        主账号被改名/停用/删除后回退该服务商任一 active 连接——否则路由会报
        "未连接"（实测踩坑：连接改名成手机号后 codebuddy_cn 全部候选失效）。
        显式指定 owner 查不到时返回 None：用户点名要这个账号，不能悄悄换号。
        需要知道本次实际选中的账号（写日志/账号级冷却）时用 pick_access_token_ex。"""
        got = await self.pick_access_token_ex(provider_code, db, owner=owner)
        return got[1] if got else None

    # ── 多账号自动选号 + 账号级冷却（2026-10-13 用户要求）──────────────
    # 多账号（同一 oauth_code 下多条连接）时：
    #   1) 自动模式不再固定用 id 最早的账号，而是优先「有余额且积分最近到期」
    #      的账号（先用快过期的，避免浪费）。只读 oauth_usage 的内存缓存——
    #      请求路径绝不同步打上游额度接口；缓存冷启动时退化为旧语义。
    #   2) 某账号请求失败（429/401/403/5xx…）后进入账号级冷却（指数退避），
    #      自动选号跳过它、改用其他账号；全部冷却时仍回退使用（避免饿死）。
    #      点名（provider.oauth_owner）的账号不受冷却影响——用户明确指定就照用。

    _ACCOUNT_COOLDOWN_CAP = 3600

    @staticmethod
    def _account_cooldown_seconds(status_code: Optional[int], fail_count: int,
                                  risk_block: bool = False) -> int:
        """失败 → 冷却秒数：429=60s 起、401/403=300s 起、其余=120s 起，指数退避封顶 1h。

        账号级风控拦截（CodeBuddy 11128 等，见 error_text.is_risk_block_text）不是瞬时
        故障，用 120s 基础档只会反复牺牲请求去试探 → 直接 30 分钟起步（仍受 1h 封顶）。"""
        if risk_block:
            base = 1800
        elif status_code == 429:
            base = 60
        elif status_code in (401, 403):
            base = 300
        else:
            base = 120
        return min(base * (2 ** max(int(fail_count or 1) - 1, 0)), OAuthClient._ACCOUNT_COOLDOWN_CAP)

    def _rank_auto_rows(self, provider_code: str, rows: list) -> list:
        """自动模式候选排序键（升序取第一）。

        频率感知分流（2026-10-16，见 core/account_balancer.py）：
          - 低频（默认）：(是否冷却, 额度档位, 恒定负载位, 最早到期, -剩余, __default, id)
            —— load 位恒 (0, 0.0)，与 F40 的「额度排序键全量参与」完全等价；
          - 分流模式（滑窗请求数达阈值自动进入）：load 位 = (窗口内请求数, 最近使用)
            —— 同档位账号之间轮流用；冷却与已耗尽账号仍排最后。
        额度键来自 oauth_usage.quota_rank_key：有余额按最早到期升序 → 无数据 →
        已耗尽。缓存冷启动时全部为「无数据」，退化为 __default 优先、其次 id 最早。"""
        from server.core.oauth_usage import cached_usage_for_token, quota_rank_key
        from server.core import account_balancer
        now = _now_utc()
        balancing = account_balancer.is_balancing(provider_code)
        ranked = []
        for row in rows:
            cooling = 1 if (row.cooldown_until and row.cooldown_until > now) else 0
            token = None
            try:
                token = self._crypto.decrypt(row.access_token_enc) if row.access_token_enc else None
            except Exception:
                token = None
            usage = cached_usage_for_token(provider_code, token) if token else None
            pref = 0 if row.owner == "__default" else 1
            tier, earliest, neg_rem = quota_rank_key(usage)
            load = (account_balancer.account_load_key(provider_code, row.owner)
                    if balancing else (0, 0.0))
            ranked.append((cooling, tier, load, earliest, neg_rem, pref, row.id, row))
        ranked.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4], t[5], t[6]))
        return ranked

    async def _row_token_ready(self, db: AsyncSession, provider_code: str, row) -> Optional[str]:
        """取该行可用的 access_token（临期主动刷新；刷新失败用旧 token 兜底）。"""
        try:
            token_plain = self._crypto.decrypt(row.access_token_enc) if row.access_token_enc else None
        except Exception:
            return None
        provider = get_oauth_provider(provider_code)
        lead = provider.refresh_lead_seconds if provider else 600
        exp = row.expires_at
        if exp:
            now = _now_utc()
            threshold = exp - timedelta(seconds=lead)
            if now >= threshold:
                refresh_ok, _ = await self.refresh_token(provider_code, db, row.owner)
                if refresh_ok:
                    refreshed = await self._get_token_record(db, provider_code, row.owner)
                    if refreshed:
                        try:
                            token_plain = self._crypto.decrypt(refreshed.access_token_enc)
                        except Exception:
                            pass
        return token_plain

    async def pick_access_token_ex(
        self,
        provider_code: str,
        db: AsyncSession,
        owner: str = "__default",
        model_id: str = "",
    ) -> Optional[Tuple[str, str]]:
        """同 pick_access_token，但返回 (实际选中的 owner, access_token)。

        显式 owner：精确取号（不受冷却影响）。
        自动模式（__default）：加载全部 active 连接 → 账号级冷却/额度到期排序 →
        依序尝试取 token（某行解密失败自动落到下一行）。
        model_id：给了就同时跳过「该模型今日额度已用完」的账号（(账号×模型)
        粒度的每日限额，见 core/quota_limit.py）——该账号的其他模型不受影响。"""
        if owner and owner != "__default":
            # 点名账号同样要过「(账号×模型) 限额」闸（2026-10-18 用户要求）：
            # 上游已明确给出恢复时间点，再发就是白烧一次必败的 402/429 往返；
            # 网关直接返回限额错误（含重置时间），到点自动恢复。
            if model_id:
                try:
                    from server.core.quota_store import active_limit_owners
                    _lim = await active_limit_owners(db, provider_code, model_id)
                except Exception:
                    _lim = {}
                if owner in _lim:
                    logger.info("oauth %s/%s: model %s quota-limited until %s (explicit owner, local reject)",
                                provider_code, owner, model_id, _lim[owner])
                    return None
            row = await self._get_token_record(db, provider_code, owner)
            if not row or not row.is_active:
                return None
            token = await self._row_token_ready(db, provider_code, row)
            if not token:
                return None
            # 该行有残留冷却/失败计数 → 预置脏标记，本次成功可清库（与自动分支一致）
            try:
                if (row.fail_count or 0) or row.cooldown_until:
                    _ACCOUNT_DIRTY.add((provider_code, row.owner))
            except Exception:
                pass
            # 点名取号同样消耗该账号 → 也计入分流计数（分流排序只作用于自动模式）
            try:
                from server.core import account_balancer
                account_balancer.note_selection(provider_code, owner)
            except Exception:
                pass
            return (owner, token)
        try:
            rows = (await db.execute(
                select(OAuthToken).where(
                    OAuthToken.provider_code == provider_code,
                    OAuthToken.is_active == True,  # noqa: E712
                ).order_by(OAuthToken.id)
            )).scalars().all()
        except Exception as e:
            logger.warning("pick access token list failed: %s", e)
            return None
        if not rows:
            return None
        ranked = self._rank_auto_rows(provider_code, rows)
        # 每日限额（(账号×模型) 粒度，2026-10-16）：该模型今日已限额的账号跳过，
        # 让其他账号顶上（上游明说「可切换其他模型」= 账号对别的模型仍可用）；
        # 全部账号都限额 → 返回 None（调用方给出明确报错，自动路由据此换模型）。
        if model_id:
            try:
                from server.core.quota_store import active_limit_owners
                limited = await active_limit_owners(db, provider_code, model_id)
            except Exception:
                limited = {}
            if limited:
                ranked = [t for t in ranked if t[7].owner not in limited]
                if not ranked:
                    logger.info("oauth %s: all accounts quota-limited for %s until %s",
                                provider_code, model_id, min(limited.values()))
                    return None
        for _cool, _tier, _load, _earliest, _neg, _pref, _rid, row in ranked:
            token = await self._row_token_ready(db, provider_code, row)
            if token:
                # 该行有 DB 残留状态（如重启前写入的冷却）→ 本次若成功应能清掉：
                # 预置脏标记，让 note_request_result(ok=True) 的常规路径也去清库。
                try:
                    if (row.fail_count or 0) or row.cooldown_until:
                        _ACCOUNT_DIRTY.add((provider_code, row.owner))
                except Exception:
                    pass
                # 计入频率感知分流（见 core/account_balancer.py）：高频自动多账号轮流，
                # 低频保持粘住。记录失败绝不影响取号本身。
                try:
                    from server.core import account_balancer
                    account_balancer.note_selection(provider_code, row.owner)
                except Exception:
                    pass
                return (row.owner, token)
        return None

    async def note_request_result(
        self,
        provider_code: str,
        owner: str,
        ok: bool,
        status_code: Optional[int] = None,
        error: str = "",
        model_id: str = "",
        provider_id: Optional[int] = None,
    ) -> None:
        """账号级健康记录（多账号路由用；永不抛异常）。

        ok=False → fail_count+1 并按错误类型设置 cooldown_until（指数退避）；
        ok=True  → 仅当该账号曾失败过（脏标记）才查库清零——成功路径零成本。

        model_id：本次请求的模型（裸 id）。带它且错误文案命中「每日限额」时
        （见 core/quota_limit.py）**不走账号冷却**，而是记 (账号×模型) 限额记录
        ——该账号的其他模型不受影响，避免整号冷却误伤。"""
        if not provider_code or not owner:
            return
        mark = (provider_code, owner)
        if ok:
            if mark not in _ACCOUNT_DIRTY:
                return
            _ACCOUNT_DIRTY.discard(mark)
        else:
            # Qoder「Billing daily count exceeded」（HTTP 403 code 110）：**账号级**
            # 今日用量上限（官方 CLI 映射 todayUsageLimitExceeded；报文没有「可切换
            # 其他模型」语义，也不带重置时间）→ 账号冷却到次日 00:00 UTC+8。
            # 不能走下面的 (账号×模型) 分支：那会按模型逐个撞 403，白烧 N 次往返。
            from server.core.quota_limit import is_qoder_daily_limit_text, next_utc8_midnight
            if is_qoder_daily_limit_text(error):
                _ACCOUNT_DIRTY.add(mark)
                await self._record_account_cooldown_until(
                    provider_code, owner, next_utc8_midnight())
                return
            # 每日限额（CodeBuddy CN/Intl 等，报文自带重置时间）：(账号×模型) 粒度，
            # 只记限额记录 + 到点自动失效；账号本身不冷却（其他模型仍可用）。
            # 也置脏标记：该账号下次在**同一模型**上成功时把限额记录清掉
            # （上游提前恢复额度时无需等 reset_at）。
            if model_id and is_daily_limit_text(error):
                _ACCOUNT_DIRTY.add(mark)
                await self._record_quota_limit(provider_code, owner, model_id, error, provider_id)
                return
            # 余额/积分耗尽（Cline 402 insufficient_credits 等）：同样记 (账号×模型)
            # 限额记录（无重置时间 → 30 分钟固定冷却，与 cline2api 一致）。
            # 这是账号余额问题：该账号的其他**计费模型**同样不可用，但同一账号上的
            # 订阅/免费模型（cline-pass/*、cline-free/*）不受影响 —— 记成
            # (账号×模型) 粒度最精确：只拦真正失败的那个模型，不误伤订阅模型。
            if model_id and is_credit_exhausted_text(error):
                _ACCOUNT_DIRTY.add(mark)
                await self._record_quota_limit(provider_code, owner, model_id, error,
                                               provider_id, default_seconds=1800)
                return
            _ACCOUNT_DIRTY.add(mark)
            if status_code is None:
                status_code = status_from_error_text(error)
        try:
            from server.db import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(OAuthToken).where(
                        OAuthToken.provider_code == provider_code,
                        OAuthToken.owner == owner,
                    ).limit(1)
                )).scalar_one_or_none()
                if not row:
                    return
                if ok:
                    row.fail_count = 0
                    row.cooldown_until = None
                    # 该账号在 model_id 上成功 → 清掉同模型限额记录（上游提前恢复
                    # 额度时无需等 reset_at 自动过期；其他模型的限额记录保留）
                    if model_id:
                        try:
                            from server.models.quota_limit import QuotaLimit
                            await db.execute(
                                sa_delete(QuotaLimit).where(
                                    QuotaLimit.provider_code == provider_code,
                                    QuotaLimit.owner == owner,
                                    QuotaLimit.model_id == model_id,
                                ))
                        except Exception:
                            pass
                    await db.commit()
                    return
                fc = int(row.fail_count or 0) + 1
                row.fail_count = fc
                risk = is_risk_block_text(error)
                secs = self._account_cooldown_seconds(status_code, fc, risk)
                row.cooldown_until = _now_utc() + timedelta(seconds=secs)
                await db.commit()
                logger.info(
                    "oauth account %s/%s entering cooldown %ss (fail#%d status=%s risk_block=%s)",
                    provider_code, owner, secs, fc, status_code, risk)
        except Exception as e:
            logger.debug("note_request_result failed: %s", e)

    async def _record_account_cooldown_until(self, provider_code: str, owner: str,
                                             until: datetime) -> None:
        """把账号冷却设到指定时间点（Qoder 账号级每日用量上限用）。

        与 _record_quota_limit 的区别：限额是**账号级**的（上游文案没有「可切换
        其他模型」语义），所以写 cooldown_until 而不是 (账号×模型) 记录——
        自动选号会直接跳过该账号，换号/次日零点自动恢复。
        同一上游账号挂在多条连接名下时，同身份的所有连接一起冷却（与限额记录
        的身份扩散一致，否则换个连接名继续打同一个已超限账号）。"""
        try:
            from server.db import AsyncSessionLocal
            _owners = [owner]
            try:
                async with AsyncSessionLocal() as _idb:
                    _rows = (await _idb.execute(
                        select(OAuthToken).where(
                            OAuthToken.provider_code == provider_code,
                            OAuthToken.is_active == True,  # noqa: E712
                        ))).scalars().all()
                _ids = {}
                for _r in _rows:
                    try:
                        _tok = self._crypto.decrypt(_r.access_token_enc) if _r.access_token_enc else ""
                    except Exception:
                        _tok = ""
                    _ids[_r.owner] = token_identity(_tok) if _tok else None
                _me = _ids.get(owner)
                if _me:
                    _owners = [o for o, ident in _ids.items() if ident == _me] or [owner]
            except Exception:
                _owners = [owner]
            async with AsyncSessionLocal() as db:
                for _own in _owners:
                    row = (await db.execute(
                        select(OAuthToken).where(
                            OAuthToken.provider_code == provider_code,
                            OAuthToken.owner == _own,
                        ).limit(1)
                    )).scalar_one_or_none()
                    if row is None:
                        continue
                    # 只延长不缩短：并发/多条错误不该把冷却改早
                    if not row.cooldown_until or until > row.cooldown_until:
                        row.cooldown_until = until
                await db.commit()
            logger.info("oauth account cooldown: %s/%s until %s UTC (daily usage limit)",
                        provider_code, owner, until.isoformat())
        except Exception as e:
            logger.debug("_record_account_cooldown_until failed: %s", e)

    async def _record_quota_limit(self, provider_code: str, owner: str, model_id: str,
                                  error: str, provider_id: Optional[int] = None,
                                  default_seconds: int = 3600) -> None:
        """upsert 一条 (账号×模型) 限额记录（reset_at 优先取上游文案里的时间点）。

        并发安全：按唯一键先查后写；撞唯一约束（并发同键插入）退化为 UPDATE 重试一次。
        解析不到时间点 → default_seconds 兜底（每日限额默认 1 小时；余额耗尽传
        1800 = 30 分钟，与 cline2api 的 402 冷却策略一致，充值后自动恢复）。

        同一上游账号可能挂了多条连接（历史重复登录 = `__default` + `account-2`
        同一邮箱）：限额是**上游账号级**的，必须给同身份的所有连接都记上，
        否则自动选号会换个连接名继续打同一个破产账号（2026-10-18 生产实证）。"""
        try:
            from server.core.quota_limit import daily_limit_reset_or_default
            from server.models.quota_limit import QuotaLimit
            reset_at = daily_limit_reset_or_default(error, default_seconds=default_seconds)
            note = (str(error or "").strip()[:500]) or ""
            from server.db import AsyncSessionLocal
            # 解析同身份的其他连接（token_identity 只读 JWT payload，不验签）
            _owners = [owner]
            try:
                async with AsyncSessionLocal() as _idb:
                    _rows = (await _idb.execute(
                        select(OAuthToken).where(
                            OAuthToken.provider_code == provider_code,
                            OAuthToken.is_active == True,  # noqa: E712
                        ))).scalars().all()
                _ids = {}
                for _r in _rows:
                    try:
                        _tok = self._crypto.decrypt(_r.access_token_enc) if _r.access_token_enc else ""
                    except Exception:
                        _tok = ""
                    _ids[_r.owner] = token_identity(_tok) if _tok else None
                _me = _ids.get(owner)
                if _me:
                    _owners = [o for o, ident in _ids.items() if ident == _me] or [owner]
            except Exception:
                _owners = [owner]
            for _try in range(2):
                try:
                    async with AsyncSessionLocal() as db:
                        for _own in _owners:
                            row = (await db.execute(
                                select(QuotaLimit).where(
                                    QuotaLimit.provider_code == provider_code,
                                    QuotaLimit.owner == _own,
                                    QuotaLimit.model_id == model_id,
                                ).limit(1)
                            )).scalar_one_or_none()
                            if row is None:
                                db.add(QuotaLimit(
                                    provider_code=provider_code, provider_id=provider_id,
                                    owner=_own, model_id=model_id, reset_at=reset_at, note=note))
                            else:
                                # 取更晚的重置时间（上游可能续报；绝不让新记录提前解锁）
                                if reset_at > (row.reset_at or datetime.min):
                                    row.reset_at = reset_at
                                row.note = note
                                if provider_id is not None:
                                    row.provider_id = provider_id
                        await db.commit()
                    break
                except Exception:
                    if _try == 1:
                        raise
                    continue  # 并发撞唯一约束 → 重试一次走 UPDATE 分支
            logger.info(
                "oauth quota limit: %s/%s model=%s until %s UTC",
                provider_code, owner, model_id, reset_at.isoformat())
        except Exception as e:
            logger.debug("_record_quota_limit failed: %s", e)

    # ── 持久化辅助 ──
    async def _get_token_record(self, db: AsyncSession, provider_code: str, owner: str) -> Optional[OAuthToken]:
        try:
            r = await db.execute(
                select(OAuthToken).where(
                    OAuthToken.provider_code == provider_code,
                    OAuthToken.owner == owner,
                ).limit(1)
            )
            return r.scalar_one_or_none()
        except Exception as e:
            logger.warning("get oauth token failed: %s", e)
            return None

    async def _get_any_active_token_record(self, db: AsyncSession, provider_code: str) -> Optional[OAuthToken]:
        """自动模式兜底：该服务商任一 active 连接（id 最早 = 最先接入的账号）。"""
        try:
            r = await db.execute(
                select(OAuthToken).where(
                    OAuthToken.provider_code == provider_code,
                    OAuthToken.is_active == True,  # noqa: E712
                ).order_by(OAuthToken.id).limit(1)
            )
            return r.scalar_one_or_none()
        except Exception as e:
            logger.warning("get any active oauth token failed: %s", e)
            return None

    async def _find_owner_by_identity(
        self, db: AsyncSession, provider_code: str, identity: str,
    ) -> Optional[str]:
        """按账号身份（token_identity）找已存在的连接 owner（2026-10-13）。

        同一账号再次登录（token 轮换 / 重新授权）应写回**已有的那条连接**
        （即使它被改过名）——否则每重登一次就多一条重复连接。解密失败、
        非 JWT（identity=None）的行跳过；找不到返回 None。
        """
        if not identity:
            return None
        try:
            rows = (await db.execute(
                select(OAuthToken).where(OAuthToken.provider_code == provider_code)
            )).scalars().all()
        except Exception as e:
            logger.warning("find owner by identity failed: %s", e)
            return None
        for row in rows:
            try:
                tok = self._crypto.decrypt(row.access_token_enc) if row.access_token_enc else ""
            except Exception:
                continue
            if token_identity(tok) == identity:
                return row.owner
        return None

    async def _next_free_owner(self, db: AsyncSession, provider_code: str) -> str:
        """给「这次登录其实换了账号」找一个新的 owner 名（与 authorize 入口同规则）。

        规则：__default 被占则依次 account-2 / account-3 …（已有则跳过）。
        只在身份守卫判定「登录账号 ≠ 连接账号」时使用 —— 这样一键连接的
        语义就从「静默覆盖」变成「自动新增一个账号」，用户无需先手动改名。
        """
        try:
            rows = (await db.execute(
                select(OAuthToken.owner).where(OAuthToken.provider_code == provider_code)
            )).scalars().all()
        except Exception as e:
            logger.warning("next free owner lookup failed: %s", e)
            return provider_code + "-new"
        existing = {r for r in rows if r}
        if "__default" not in existing:
            return "__default"
        n = 2
        while f"account-{n}" in existing:
            n += 1
        return f"account-{n}"

    async def _save_token(
        self, db: AsyncSession, provider_code: str, owner: str,
        tok: dict, update_existing: Optional[OAuthToken] = None,
        allow_identity_change: bool = False,
    ) -> OAuthToken:
        """落库 token。**默认拒绝把一条连接改成另一个账号**（见 token_identity 说明）。

        `allow_identity_change=True` 仅用于「用户明确要替换这条连接的账号」的路径
        （如导入 token 且显式确认），默认关闭 —— 静默换号正是 2026-09-22 的事故成因。
        """
        expires_in = int(tok.get("expires_in") or 3600)
        refresh_expires_in = int(tok.get("refresh_token_expires_in") or tok.get("refresh_expires_in") or 0)
        access = tok.get("access_token", "")
        refresh = tok.get("refresh_token", "")
        token_type = tok.get("token_type", "Bearer")
        scope = tok.get("scope", "")
        now = _now_utc()
        expires_at = now + timedelta(seconds=expires_in)
        refresh_expires_at = (now + timedelta(seconds=refresh_expires_in)) if refresh_expires_in else None
        enc_access = self._crypto.encrypt(access)
        enc_refresh = self._crypto.encrypt(refresh) if refresh else None
        if update_existing:
            row = update_existing
        else:
            row = await self._get_token_record(db, provider_code, owner)
        # 身份守卫：同一条连接写入**另一个账号**的凭证 → 拒绝（宁可报错让用户改名，
        # 也不能静默丢掉原账号凭据）。刷新路径（update_existing）带的是同一账号的
        # 新 token，身份一致自然放行；身份取不到（非 JWT）不参与判定。
        if row and not allow_identity_change:
            new_id = token_identity(access)
            old_id = token_identity(self._crypto.decrypt(row.access_token_enc)) if row.access_token_enc else None
            if new_id and old_id and new_id != old_id:
                raise ValueError(
                    f"该连接（{provider_code}/{owner}）已属于账号「{old_id}」，"
                    f"不能写入「{new_id}」的凭证 —— 请改用「新增账号」，"
                    f"或先把这条连接改名/断开")
        if row:
            row.access_token_enc = enc_access
            row.refresh_token_enc = enc_refresh or row.refresh_token_enc
            row.token_type = token_type
            row.scope = scope
            row.expires_at = expires_at
            row.refresh_expires_at = refresh_expires_at
            row.is_active = True
            row.last_refreshed_at = now
            row.last_error = ""
        else:
            row = OAuthToken(
                provider_code=provider_code, owner=owner,
                access_token_enc=enc_access, refresh_token_enc=enc_refresh,
                token_type=token_type, scope=scope,
                expires_at=expires_at, refresh_expires_at=refresh_expires_at,
                is_active=True, last_refreshed_at=now,
            )
            db.add(row)
        await db.commit()
        await db.refresh(row)
        # 连接落库即自动登记服务商（服务商列表可见，credential_type=oauth）
        try:
            await self._ensure_provider_registered(db, provider_code)
        except Exception as e:
            logger.warning("auto-register oauth provider %s failed: %s", provider_code, e)
        return row

    async def _ensure_provider_registered(self, db: AsyncSession, provider_code: str):
        """按注册表把 OAuth provider 幂等地建成服务商行（oauth_code 或同名已存在则跳过）。"""
        from server.models.provider import Provider
        provider = (await db.execute(
            select(Provider).where(Provider.oauth_code == provider_code).limit(1)
        )).scalar_one_or_none()
        if provider:
            return
        cfg = get_oauth_provider(provider_code)
        if not cfg:
            return
        name = (cfg.name or provider_code).strip() or provider_code
        # 注册表声明的「必须走代理」（如 Meta Muse：出口按国别封锁）→ 建行即置
        # proxy_enabled=True，推理/刷新才会带 __proxy_force 走代理池。
        _force_proxy = bool((cfg.extra_params or {}).get("force_proxy"))
        dup = (await db.execute(
            select(Provider).where(Provider.name == name).limit(1)
        )).scalar_one_or_none()
        if dup:
            # 用户已手工建过同名服务商：只补 oauth 指向
            _changed = False
            if not dup.oauth_code:
                dup.oauth_code = provider_code
                dup.credential_type = "oauth"
                _changed = True
            if _force_proxy and not dup.proxy_enabled:
                dup.proxy_enabled = True
                _changed = True
            if _changed:
                await db.commit()
            return
        db.add(Provider(
            name=name,
            base_url=cfg.api_base_url or "",
            api_type=cfg.adapter_api_type or "openai_compat",
            credential_type="oauth",
            oauth_code=provider_code,
            proxy_enabled=_force_proxy,
            enabled=True,
            description=f"由 OAuth 连接自动登记（{provider_code}）",
        ))
        await db.commit()
        logger.info("oauth provider auto-registered: %s", provider_code)

    async def get_token_meta(self, provider_code: str, db: AsyncSession,
                             owner: str = "__default") -> dict:
        """连接记录的 scope 列若为 JSON（qoder 存 COSY 签名元数据）则解析返回，否则 {}。"""
        row = await self._get_token_record(db, provider_code, owner)
        if not row or not row.scope:
            return {}
        try:
            import json as _json
            data = _json.loads(row.scope)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # 用于 admin 端点列出所有 oauth 连接
    async def list_connections(self, db: AsyncSession) -> list:
        r = await db.execute(select(OAuthToken).order_by(OAuthToken.id))
        rows = r.scalars().all()
        # 每日限额记录（(账号×模型) 粒度）：按 (provider_code, owner) 分组附到连接行
        _qmap: dict = {}
        try:
            from server.core.quota_store import list_active
            for ql in await list_active(db):
                _qmap.setdefault((ql.provider_code, ql.owner), []).append({
                    "model_id": ql.model_id,
                    "reset_at": ql.reset_at.isoformat() + "Z",
                })
        except Exception:
            pass
        out = []
        for row in rows:
            out.append({
                "id": row.id,
                "provider_code": row.provider_code,
                "owner": row.owner,
                "token_type": row.token_type,
                "scope": row.scope,
                "is_active": row.is_active,
                "expires_at": row.expires_at.isoformat() + "Z" if row.expires_at else None,
                "refresh_expires_at": row.refresh_expires_at.isoformat() + "Z" if row.refresh_expires_at else None,
                "last_refreshed_at": row.last_refreshed_at.isoformat() + "Z" if row.last_refreshed_at else None,
                "last_error": row.last_error,
                "cooldown_until": row.cooldown_until.isoformat() + "Z" if getattr(row, "cooldown_until", None) else None,
                "fail_count": int(getattr(row, "fail_count", 0) or 0),
                "quota_limits": _qmap.get((row.provider_code, row.owner), []),
            })
        return out


# Single Flight 用的 state → verifier map
OAuthClient._state_verifier_map: Dict[str, str] = {}


_client: Optional[OAuthClient] = None


def get_oauth_client() -> OAuthClient:
    global _client
    if _client is None:
        _client = OAuthClient()
    return _client
