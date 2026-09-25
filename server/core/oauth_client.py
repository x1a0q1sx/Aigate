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
import logging
import secrets
import time
import httpx
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from server.db import AsyncSessionLocal
from server.models.oauth_token import OAuthToken
from server.core.crypto_service import get_crypto_service, CryptoService
from server.core.oauth_registry import OAuthProviderConfig, get_oauth_provider

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.utcnow()


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
        if not existing or not existing.refresh_token_enc:
            return False, "no refresh_token stored"
        refresh_plain = self._crypto.decrypt(existing.refresh_token_enc)
        # ── 非标准刷新协议按 extra_params.refresh_style 分发 ──
        _style = (provider.extra_params or {}).get("refresh_style")
        if _style == "none":
            # 长期凭证（如 u1s1 api_key）：无标准刷新，到期重登录即可
            return True, "long-lived credential (re-login when it expires)"
        if _style == "codebuddy" or provider_code == "codebuddy_cn":
            # 腾讯系（CN/国际服同构）：X-Refresh-Token 头 + 空 JSON body
            return await self._refresh_codebuddy(db, provider, existing, refresh_plain)
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
        # ── Qoder device_token ──
        if provider_code == "qoder" and (provider.extra_params or {}).get("device_code_only"):
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

    # ── Qoder 设备流（本地 PKCE+nonce，轮询 deviceToken/poll 收 dt- token） ──
    async def start_qoder_device(self, provider_code: str, db: AsyncSession,
                                 owner: str = "__default") -> dict:
        """对齐 9router QoderService.initiateDeviceFlow：
        1) 本地生成 PKCE(S256) verifier/challenge + nonce + machine_id
        2) 用户浏览器打开 qoder.com/device/selectAccounts?challenge&nonce&machine_id
        3) 后台轮询 openapi.qoder.sh/api/v1/deviceToken/poll（202/404=pending）
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
            asyncio.create_task(self._poll_qoder_device(
                provider_code, nonce, verifier, machine_id, owner))
            return {"state": nonce, "login_url": login_url,
                    "poll_interval_ms": 2000, "owner": owner}
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
                    await self._save_token(db, provider_code, owner, tok)
                    logger.info("codebuddy %s token acquired after %d polls",
                                  provider_code, attempt + 1)
                    return
            except Exception as e:
                logger.warning("codebuddy poll attempt %d failed: %s", attempt + 1, e)
                continue
        logger.warning("codebuddy poll for state %s reached max_attempts without token",
                        state)

    async def pick_access_token(
        self,
        provider_code: str,
        db: AsyncSession,
        owner: str = "__default",
    ) -> Optional[str]:
        """供 v1_router 使用：返回有效 access_token；必要时主动刷新。

        owner=__default（自动模式）时，主账号被改名/停用/删除后回退该服务商
        任一 active 连接（id 最早）——否则路由会报"未连接"（实测踩坑：连接
        改名成手机号后 codebuddy_cn 全部候选失效）。显式指定 owner 查不到
        时返回 None：用户点名要这个账号，不能悄悄换号。"""
        existing = await self._get_token_record(db, provider_code, owner)
        if (not existing or not existing.is_active) and owner == "__default":
            existing = await self._get_any_active_token_record(db, provider_code)
            if existing:
                owner = existing.owner
        if not existing or not existing.is_active:
            return None
        # 解密
        token_plain = self._crypto.decrypt(existing.access_token_enc)
        # 检查是否到期前需要刷新
        provider = get_oauth_provider(provider_code)
        lead = provider.refresh_lead_seconds if provider else 600
        exp = existing.expires_at
        if exp:
            now = _now_utc()
            threshold = exp - timedelta(seconds=lead)
            if now >= threshold:
                refresh_ok, _ = await self.refresh_token(provider_code, db, owner)
                if refresh_ok:
                    refreshed = await self._get_token_record(db, provider_code, owner)
                    if refreshed:
                        token_plain = self._crypto.decrypt(refreshed.access_token_enc)
                # 刷新失败也用旧 token 试一次（兜底）
        return token_plain

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

    async def _save_token(
        self, db: AsyncSession, provider_code: str, owner: str,
        tok: dict, update_existing: Optional[OAuthToken] = None,
    ) -> OAuthToken:
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
        dup = (await db.execute(
            select(Provider).where(Provider.name == name).limit(1)
        )).scalar_one_or_none()
        if dup:
            # 用户已手工建过同名服务商：只补 oauth 指向
            if not dup.oauth_code:
                dup.oauth_code = provider_code
                dup.credential_type = "oauth"
                await db.commit()
            return
        db.add(Provider(
            name=name,
            base_url=cfg.api_base_url or "",
            api_type=cfg.adapter_api_type or "openai_compat",
            credential_type="oauth",
            oauth_code=provider_code,
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
