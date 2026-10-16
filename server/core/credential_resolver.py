"""统一凭证/执行通道解析器（v2 回退修复 D+E）。

给定 (provider, model, db)，解析该候选的调用方式，消除 combo / auto cascade /
direct 三处各自为政的 key 选择逻辑：

- credential_type=free_tier → free executor（9Router 自定义协议，无需密钥）
- credential_type=oauth     → OAuth client pick_access_token（自动刷新）
- api_type=atomcode         → atomcode daemon 通道（无需密钥）
- 标准 api_key              → key_rotator 按模型归属选 key，兜底第一把 active key

resolve() 返回 ResolvedCredential：
- error 非 None → 该候选不可用（调用方记录并回退）
- free_tier → 用 stream()/call() 统一入口（内部处理 _FORCE_PROXY 与裸 model_id）
- 其他 kind → adapter + api_key + extra_headers，调用方照常调 adapter
"""
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from server.core.model_catalog import create_adapter_for_provider
from server.core.error_text import is_risk_block_text

logger = logging.getLogger(__name__)


@dataclass
class ResolvedCredential:
    kind: str                       # standard | oauth | free_tier | atomcode
    api_key: str = ""
    key_id: Optional[int] = None
    adapter = None                  # BaseAdapter（free_tier 为 None，走 free executor）
    extra_headers: Optional[dict] = None
    free_executor = None
    free_code: str = ""
    base_url: str = ""
    account: str = ""               # OAuth 多账号：本次实际选中的账号（owner），写日志/账号冷却用
    oauth_code: str = ""            # OAuth registry code（账号冷却寻址用）
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def error_status_code(e: BaseException) -> Optional[int]:
    """尽力从异常链里提取 HTTP 状态码（供账号级冷却分级）。

    顺序：异常自身 status_code → 其 response.status_code → __cause__/__context__
    链 → 文案中的 4xx/5xx 数字。提取不到返回 None。"""
    cur = e
    for _ in range(5):
        if cur is None:
            break
        sc = getattr(cur, "status_code", None)
        if sc is None:
            sc = getattr(getattr(cur, "response", None), "status_code", None)
        if sc:
            try:
                return int(sc)
            except (TypeError, ValueError):
                pass
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    try:
        from server.core.oauth_client import status_from_error_text
        return status_from_error_text(str(e))
    except Exception:
        return None


def is_account_worthy_error(error, status_code: Optional[int] = None) -> bool:
    """该失败是否值得记到「账号级冷却」（账号故障 vs 内容/请求问题）。

    真：401/403/429、5xx、含限流语义的错误文案（rate limit / too many requests /
    quota / usage limit / FreeUsageLimitError）、上游账号级风控拦截（CodeBuddy 11128
    等，is_risk_block_text）——说明这个账号现在打不通，换号有用。
    假：空输出、race 取消、上下文超限（调用方已排除）等——模型/内容侧问题，
    把账号冷却了反而白白减少可用账号。"""
    try:
        sc = status_code if status_code is not None else error_status_code(
            error if isinstance(error, BaseException) else Exception(str(error)))
    except Exception:
        sc = status_code
    try:
        if sc is not None:
            sc = int(sc)
            if sc in (401, 403, 429) or sc >= 500:
                return True
    except (TypeError, ValueError):
        pass
    text = str(error or "").lower()
    if is_risk_block_text(text):
        return True
    return any(k in text for k in (
        "rate limit", "too many requests", "quota", "usage limit",
        "freeusagelimiterror", "usage exceeds frequency"))


async def note_account_result(provider, account: str, ok: bool,
                              status_code: Optional[int] = None, error: str = "",
                              model_id: str = "") -> None:
    """把一次上游调用的结果记到 OAuth 账号（失败→账号级冷却；成功→清零）。

    仅对 OAuth 多账号路由有意义；account 为空（标准 key/free_tier/未选号）直接跳过。
    失败时先过 `is_account_worthy_error` 闸：空输出/内容校验类失败不冷却账号。
    model_id：本次请求的模型（裸 id）——错误命中「每日限额」文案时改记
    (账号×模型) 限额记录而非账号冷却（见 oauth_client.note_request_result）。
    永不抛异常、永不阻塞主流程（内部吞掉一切错误）。"""
    if not account:
        return
    if not ok:
        # 每日限额（带重置时间的文案）必须记录，不受常规资格闸约束；
        # 其余失败仍先过 is_account_worthy_error 闸：空输出/内容校验类不冷却账号。
        from server.core.quota_limit import is_daily_limit_text
        if not (model_id and is_daily_limit_text(error)) \
                and not is_account_worthy_error(error, status_code):
            return
    try:
        from server.core.oauth_client import get_oauth_client
        code = getattr(provider, "oauth_code", None) if provider is not None else None
        if not code and provider is not None:
            code = getattr(provider, "name", None)
        if not code:
            return
        await get_oauth_client().note_request_result(
            code, account, ok=ok, status_code=status_code, error=error or "",
            model_id=model_id or "",
            provider_id=getattr(provider, "id", None) if provider is not None else None)
    except Exception as e:
        logger.debug("note_account_result failed: %s", e)


async def resolve_credential_async(provider, model, db: AsyncSession) -> ResolvedCredential:
    """解析候选的凭证与执行通道。永不抛异常，失败通过 error 字段表达。"""
    cred_type = getattr(provider, "credential_type", "api_key") or "api_key"
    api_type = getattr(provider, "api_type", "openai_compat") or ""
    rc = ResolvedCredential(kind="standard", base_url=provider.base_url or "")

    # free_tier / oauth / atomcode：无需 api_keys 表密钥
    if cred_type in ("free_tier", "oauth") or api_type == "atomcode":
        if api_type == "atomcode":
            rc.kind = "atomcode"
            rc.api_key = ""
            rc.adapter = create_adapter_for_provider("atomcode")
            return rc
        if cred_type == "oauth":
            rc.kind = "oauth"
            from server.core.oauth_client import get_oauth_client
            from server.core.oauth_registry import get_oauth_provider as _get_oauth_p
            # 优先 provider.oauth_code 显式指向 OAuthRegistry；老数据回退 provider.name
            oauth_code = getattr(provider, "oauth_code", None) or provider.name
            oauth_p = _get_oauth_p(oauth_code)
            # v4.2: 多账号连接时按 provider.oauth_owner 点名取号；
            # 空=自动（多账号时按「余额最近到期 + 账号冷却」排序选号，见 pick_access_token_ex）
            owner = (getattr(provider, "oauth_owner", None) or "").strip() or "__default"
            # model_id 供选号时跳过「该模型今日额度已用完」的账号（(账号×模型) 限额，
            # 2026-10-16）；该账号的其他模型不受影响
            got = (await get_oauth_client().pick_access_token_ex(
                       oauth_code, db, owner=owner,
                       model_id=getattr(model, "model_id", "") or "")
                   if oauth_p else None)
            if not got:
                _err = (f"OAuth provider '{oauth_code}' not connected"
                        if owner == "__default"
                        else f"OAuth provider '{oauth_code}' account '{owner}' not connected")
                # 区分「全部账号该模型今日额度已用完」：自动路由应换模型，而非误报未连接
                # （(账号×模型) 每日限额，2026-10-16）
                _mid = getattr(model, "model_id", "") or ""
                if owner == "__default" and _mid:
                    try:
                        from server.core.quota_store import active_limit_owners
                        _lim = await active_limit_owners(db, oauth_code, _mid)
                        if _lim:
                            from sqlalchemy import select as _sel
                            from server.models.oauth_token import OAuthToken as _OT
                            _rows = (await db.execute(_sel(_OT).where(
                                _OT.provider_code == oauth_code,
                                _OT.is_active == True))).scalars().all()  # noqa: E712
                            if _rows and all(r.owner in _lim for r in _rows):
                                _latest = max(_lim.values())
                                _err = (f"quota_limited: model '{_mid}' daily quota exhausted on "
                                        f"all accounts; resets at {_latest.isoformat()}Z")
                    except Exception:
                        pass
                rc.error = _err
                return rc
            rc.account, token = got[0], got[1]
            rc.oauth_code = oauth_code
            rc.api_key = token
            # oauth 请求需要 __oauth 标记（adapter 侧据此用 Bearer token 并处理专属头）
            from server.api.v1_router import _merge_oauth_headers
            rc.extra_headers = _merge_oauth_headers(provider, getattr(provider, "headers", None))
            # u1s1：登录时若已持久化设备密钥，则切换为官方 DPoP 形态
            #（Authorization: DPoP <u1s1d-…> + 逐请求 dpop proof），赠送额度才放行
            if oauth_code == "u1s1":
                # 多账号：签名材料必须跟随**实际选中的账号**（rc.account），
                # 否则 A 账号的 token 配 B 账号的设备密钥 → DPoP 验签失败
                try:
                    material = await get_oauth_client().get_device_signing_material(
                        oauth_code, db, owner=(rc.account or owner))
                except Exception:
                    material = None
                if material:
                    rc.api_key = material["bearer"]
                    # 客户端证明（完整性审查第二段）：官方 CLI 从 /v1/models 响应领 token，
                    # 推理请求逐条附 x-u1s1-attestation；缺了它 DPoP 过了照样 403
                    from server.core.u1s1_attestation import get_attestation
                    try:
                        att = await get_attestation(rc.base_url)
                    except Exception:
                        att = None
                    rc.extra_headers = {**(rc.extra_headers or {}),
                                        "__dpop": {"token": material["bearer"],
                                                   "jwk": material["priv"],
                                                   "att": att}}
            rc.adapter = create_adapter_for_provider(api_type)
            # LobsterAI：X-LobsterAI-Client-Version 是**动态真值**（12h 缓存 + 兜底），
            # quirks 的静态头表放不下 → 在此注入（缺它会少 kimi-k3 等模型）
            if oauth_code == "lobsterai":
                try:
                    from server.core.lobsterai import resolve_client_version
                    rc.extra_headers = {**(rc.extra_headers or {}),
                                        "X-LobsterAI-Client-Version":
                                            await resolve_client_version()}
                except Exception:
                    pass
            # CodeArts：**不是 Bearer** —— 推理要 AK/SK/SecurityToken 签名，
            # access_token 列里存的是凭据 JSON。校验格式（裸 token 发出去必然
            # 验签 401，报错还指向「签名头错位」这种看不出根因的地方）。
            if oauth_code == "codearts":
                import json as _json
                try:
                    _cred = _json.loads(rc.api_key)
                    if not isinstance(_cred, dict) or not _cred.get("access_key_id"):
                        rc.error = "codearts 凭据格式不对（应为 JSON 凭据），请重新连接"
                        return rc
                except (ValueError, TypeError):
                    rc.error = "codearts 凭据不是 JSON（请重新连接该账号）"
                    return rc
            return rc
        # free_tier
        rc.kind = "free_tier"
        from server.core.free_providers import get_free_executor, resolve_free_code
        free_code = resolve_free_code(provider.name, getattr(provider, "oauth_code", None))
        free_exec = get_free_executor(free_code) if free_code else None
        if not free_exec:
            rc.error = (f"free_tier provider '{provider.name}' has no matching executor"
                        f" (free_code={free_code})")
            return rc
        rc.free_executor = free_exec
        rc.free_code = free_code
        rc.api_key = ""
        return rc

    # 标准路径：按模型归属 key 集合选（多则轮询、单则用一、无归属 fallback 第一把 active）
    try:
        from server.core.key_rotator import get_key_rotator
        picked = await get_key_rotator().pick_key_for_model(db, model)
    except Exception as e:
        logger.warning("key rotator failed for %s/%s: %s", provider.name, getattr(model, "model_id", "?"), e)
        picked = None
    if picked and picked[0] is not None:
        rc.key_id, rc.api_key = picked
    else:
        from server.models.api_key import ApiKey
        from server.core.crypto_service import get_crypto_service
        from sqlalchemy import select as _select
        key = (await db.execute(
            _select(ApiKey).where(ApiKey.provider_id == provider.id, ApiKey.is_active == True).limit(1)  # noqa: E712
        )).scalar_one_or_none()
        if not key:
            rc.error = f"No active API key for provider {provider.name}"
            return rc
        rc.key_id = key.id
        rc.api_key = get_crypto_service().decrypt(key.key_encrypted)
    # 与 oauth 分支/推理旧路径对齐：标准 api_key 也要合并服务商头与路由标记
    # （__proxy_force 等）。否则服务商开「走代理」只对旧调用点生效，经统一解析器的
    # /v1 直连仍裸直发，国别封锁站 403（2026-10-09 tokenharbor 实证）。
    from server.api.v1_router import _merge_oauth_headers
    rc.extra_headers = _merge_oauth_headers(provider, getattr(provider, "headers", None))
    rc.adapter = create_adapter_for_provider(api_type)
    return rc


async def stream_via(resolved: ResolvedCredential, request, provider, model):
    """统一流式入口：返回 chunk 异步迭代器。

    free_tier 内部处理 _FORCE_PROXY 上下文与裸 model_id；其余走 adapter.stream_chat_completion。
    """
    if resolved.kind == "free_tier":
        from server.api.v1_router import _without_unsupported_reasoning
        from server.core.proxy_pool import FORCE_PROXY
        free_req = _without_unsupported_reasoning(
            request.model_copy(update={"model": model.model_id}), model
        )
        proxy_token = FORCE_PROXY.set(bool(getattr(provider, "proxy_enabled", False)))

        async def _gen():
            try:
                async for ck in resolved.free_executor.execute_stream(free_req):
                    yield ck
            finally:
                FORCE_PROXY.reset(proxy_token)
        return _gen()

    extra_headers = resolved.extra_headers
    if extra_headers is None:
        extra_headers = _provider_extra_headers(provider)
    return resolved.adapter.stream_chat_completion(
        request.model_copy(update={"model": model.model_id}),
        resolved.api_key,
        provider.base_url,
        extra_headers,
    )


async def call_via(resolved: ResolvedCredential, request, provider, model) -> dict:
    """统一非流式入口：返回完整 OpenAI 格式 dict。"""
    if resolved.kind == "free_tier":
        from server.api.v1_router import _without_unsupported_reasoning
        from server.core.proxy_pool import FORCE_PROXY
        free_req = _without_unsupported_reasoning(
            request.model_copy(update={"model": model.model_id}), model
        )
        proxy_token = FORCE_PROXY.set(bool(getattr(provider, "proxy_enabled", False)))
        try:
            return await resolved.free_executor.execute_non_stream(free_req)
        finally:
            FORCE_PROXY.reset(proxy_token)

    extra_headers = resolved.extra_headers
    if extra_headers is None:
        extra_headers = _provider_extra_headers(provider)
    return await resolved.adapter.chat_completion(
        request.model_copy(update={"model": model.model_id}),
        resolved.api_key,
        provider.base_url,
        extra_headers,
    )


def _provider_extra_headers(provider) -> Optional[dict]:
    """provider.headers 合并 OAuth 标记（v1_router._merge_oauth_headers 的薄包装）。"""
    try:
        from server.api.v1_router import _merge_oauth_headers
        return _merge_oauth_headers(provider, getattr(provider, "headers", None))
    except Exception:
        return getattr(provider, "headers", None) or None
