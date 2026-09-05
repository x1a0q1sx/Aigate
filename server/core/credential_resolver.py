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
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


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
            token = await get_oauth_client().pick_access_token(oauth_code, db) if oauth_p else None
            if not token:
                rc.error = f"OAuth provider '{oauth_code}' not connected"
                return rc
            rc.api_key = token
            # oauth 请求需要 __oauth 标记（adapter 侧据此用 Bearer token 并处理专属头）
            from server.api.v1_router import _merge_oauth_headers
            rc.extra_headers = _merge_oauth_headers(provider, getattr(provider, "headers", None))
            rc.adapter = create_adapter_for_provider(api_type)
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
