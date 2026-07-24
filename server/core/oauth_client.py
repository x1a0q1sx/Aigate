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


def gen_pkce_pair() -> Tuple[str, str]:
    """生成 (code_verifier, code_challenge) — S256 method"""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def gen_state() -> str:
    return secrets.token_urlsafe(32)


class OAuthClient:
    """OAuth 客户端 + 主动刷新 + 持久化"""

    def __init__(self, crypto: CryptoService = None, redirect_override: str = None):
        self._crypto = crypto or get_crypto_service()
        self.redirect_override = redirect_override         # 运行时 host 替换默认 redirect
        self._flight_locks: Dict[str, asyncio.Lock] = {}    # Single Flight per (provider+owner)
        self._inflight: Dict[str, asyncio.Future] = {}       # 进行中 refresh 的 future

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
            try:
                await asyncio.wait_for(self._inflight[lock_key], timeout=15)
                return True, "merged into inflight refresh"
            except asyncio.TimeoutError:
                return False, "previous refresh timed out"

        fut = asyncio.get_event_loop().create_future()
        self._inflight[lock_key] = fut
        try:
            res = await self._do_refresh(db, provider_code, owner)
            fut.set_result(res)
            return res
        except Exception as e:
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
        # ── CodeBuddy CN 非标准协议：X-Refresh-Token 头 + 空 JSON body ──
        if provider_code == "codebuddy_cn":
            return await self._refresh_codebuddy(db, provider, existing, refresh_plain)
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
            # refresh_token 失效时标记已有 token 失效
            existing.last_error = err
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
                existing.last_error = err
                existing.is_active = False
                await db.commit()
                return False, f"http {resp.status_code}: {err}"
            data = resp.json()
            # 腾讯返回：{ "code": 0, "data": { "accessToken": "...", "refreshToken": "...", "expiresIn": 3600 } }
            if data.get("code") != 0 or not data.get("data", {}).get("accessToken"):
                err = data.get("msg") or "no accessToken in response"
                existing.last_error = err
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

    # ── CodeBuddy CN 专属 device_poll 流程：首次登录 ──
    async def start_device_poll(
        self,
        provider_code: str,
        db: AsyncSession,
        owner: str = "__default",
    ) -> dict:
        """
        CodeBuddy CN / 类似的 device poll provider：
        1) POST state_url 拿一个 state
        2) 返回 state + login_url 给前端弹窗
        3) 同时启动后台轮询，每 poll_interval_ms 拿 token_url?state=xxx
        4) 拿到 token 后持久化
        返回 {"state": "...", "login_url": "https://copilot.tencent.com/...?state=xxx",
              "poll_interval_ms": 5000, "owner": "..."
        }
        """
        provider = get_oauth_provider(provider_code)
        if not provider:
            return {"error": f"unknown provider {provider_code}"}
        ep = provider.extra_params or {}
        if ep.get("auth_mode") != "device_poll":
            return {"error": "provider not device_poll mode"}
        state_url = ep.get("state_url") or provider.token_url
        ua = ep.get("user_agent", "CLI/2.63.2 CodeBuddy/2.63.2")
        headers = {
            "Accept": "application/json",
            "User-Agent": ua,
            "X-Requested-With": "XMLHttpRequest",
            "X-Product": ep.get("x_product", "SaaS"),
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(state_url, headers=headers)
            if resp.status_code >= 400:
                return {"error": f"state URL HTTP {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()
            # 期望返回：{ "code": 0, "data": { "state": "xxxx", "loginUrl": "https://..." } }
            inner = data.get("data") if data.get("code") == 0 else data
            state = inner.get("state") if isinstance(inner, dict) else None
            login_url = (inner.get("loginUrl") if isinstance(inner, dict) else None) or \
                        (state_url + ("?state=" + state if state else ""))
            if not state:
                return {"error": f"missing state in response: {resp.text[:200]}"}
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
        拿到 token 后持久化到 DB。
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
                    inner = data.get("data") if data.get("code") == 0 else data
                    if not isinstance(inner, dict):
                        continue
                    access = inner.get("accessToken")
                    if not access:
                        # 还在等待用户登录，继续轮询
                        continue
                    # 拿到 token — 适配 _save_token 标准字段
                    tok = {
                        "access_token": access,
                        "refresh_token": inner.get("refreshToken", ""),
                        "expires_in": inner.get("expiresIn") or 3600,
                        "token_type": "Bearer",
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
        """供 v1_router 使用：返回有效 access_token；必要时主动刷新"""
        existing = await self._get_token_record(db, provider_code, owner)
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
        return row

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
