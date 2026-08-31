"""
模型目录服务
管理模型元数据、刷新、auto 候选选择
v2.0: 支持 priority_boost + auto_excluded
"""
import logging
import asyncio
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update, delete
from server.models.provider import Provider
from server.models.model import Model
from server.models.api_key import ApiKey
from server.models.model_api_key import ModelApiKey  # v3.5 模型级密钥归属关联表
from server.adapters.base_adapter import BaseAdapter, ModelInfo
from server.adapters.openai_compat import OpenAICompatAdapter
from server.adapters.codex_responses import CodexResponsesAdapter
from server.adapters.anthropic_adapter import AnthropicAdapter
from server.adapters.github_adapter import GitHubAdapter
from server.adapters.image_adapter import ImageAdapter
from server.adapters.atomcode_adapter import AtomCodeAdapter
from server.adapters.xyusec_pricing import fetch_provider_pricing, match_model_metadata
from .key_manager import KeyManager
from server.config import get_config

logger = logging.getLogger(__name__)
# 内置价格参考表 (美元 / 百万 tokens)
# fmt: off
BUILTIN_PRICING = {
    # OpenAI
    "gpt-4o":                {"input": 5.0, "output": 15.0, "is_free": False},
    "gpt-4o-mini":           {"input": 0.15, "output": 0.6, "is_free": False},
    "gpt-4-turbo":           {"input": 10.0, "output": 30.0, "is_free": False},
    "gpt-3.5-turbo":         {"input": 0.5, "output": 1.5, "is_free": False},
    # DeepSeek
    "deepseek-chat":         {"input": 0.14, "output": 0.28, "is_free": False},
    "deepseek-coder":        {"input": 0.14, "output": 0.28, "is_free": False},
    # Groq 免费模型
    "llama3-8b-8192":        {"input": 0, "output": 0, "is_free": True},
    "llama3-70b-8192":       {"input": 0, "output": 0, "is_free": True},
    "mixtral-8x7b-32768":   {"input": 0, "output": 0, "is_free": True},
    "gemma-7b-it":           {"input": 0, "output": 0, "is_free": True},
    # 通义千问
    "qwen-turbo":            {"input": 0.03, "output": 0.06, "is_free": False},
    "qwen-plus":             {"input": 0.4, "output": 0.8, "is_free": False},
    "qwen-max":              {"input": 0.8, "output": 1.6, "is_free": False},
    # 智谱
    "glm-4":                 {"input": 1.0, "output": 1.0, "is_free": False},
    "glm-3-turbo":           {"input": 0, "output": 0, "is_free": True},
    # Moonshot
    "moonshot-v1-8k":        {"input": 0.012, "output": 0.012, "is_free": False},
    "moonshot-v1-32k":       {"input": 0.024, "output": 0.024, "is_free": False},
    "moonshot-v1-128k":      {"input": 0.06, "output": 0.06, "is_free": False},
}
# fmt: on
def get_builtin_pricing(model_id: str) -> Optional[dict]:
    """匹配内置定价表"""
    if model_id in BUILTIN_PRICING:
        return BUILTIN_PRICING[model_id]
    for key, pricing in BUILTIN_PRICING.items():
        if key in model_id:
            return pricing
    return None
def create_adapter_for_provider(api_type: str, timeout: Optional[int] = None) -> BaseAdapter:
    """根据 api_type 创建适配器。
    timeout 为可选项：传入时覆盖适配器默认超时（用于刷新模型等后台网络请求）。"""
    if api_type in ("anthropic", "claude_code"):
        return AnthropicAdapter(timeout=timeout) if timeout else AnthropicAdapter()
    elif api_type == "github":
        return GitHubAdapter(timeout=timeout) if timeout else GitHubAdapter()
    elif api_type == "image":
        return ImageAdapter(timeout=timeout) if timeout else ImageAdapter()
    elif api_type == "openai_compat":
        return OpenAICompatAdapter(timeout=timeout) if timeout else OpenAICompatAdapter()
    elif api_type == "codex_responses":
        return CodexResponsesAdapter(timeout=timeout) if timeout else CodexResponsesAdapter()
    elif api_type == "atomcode":
        return AtomCodeAdapter(timeout=timeout) if timeout else AtomCodeAdapter()
    else:
        return OpenAICompatAdapter(timeout=timeout) if timeout else OpenAICompatAdapter()
class ModelCatalog:
    """模型目录服务"""
    def __init__(self):
        pass
    async def list_models(
        self,
        session: AsyncSession,
        provider_id: Optional[int] = None,
        is_free: Optional[bool] = None,
        auto_enabled: Optional[bool] = None,
        enabled_only: bool = True,
        extra_conditions: Optional[list] = None
    ) -> List[Model]:
        """列出模型，支持过滤；extra_conditions 可追加调用方自带的 SQL 条件（如模糊搜索）"""
        conditions = []
        if enabled_only:
            conditions.append(Model.enabled == True)
        if provider_id is not None:
            conditions.append(Model.provider_id == provider_id)
        if is_free is not None:
            conditions.append(Model.is_free == is_free)
        if auto_enabled is not None:
            conditions.append(Model.auto_enabled == auto_enabled)
        if extra_conditions:
            conditions.extend(extra_conditions)
        query = select(Model)
        if enabled_only:
            # v4.0: 服务商被禁用时其模型同样不参与任何请求（但仍保留在 DB 中）
            query = query.join(Provider, Model.provider_id == Provider.id)
            conditions.append(Provider.enabled == True)
        if conditions:
            query = query.where(and_(*conditions))
        query = query.order_by(Model.provider_id, Model.model_id)
        result = await session.execute(query)
        return list(result.scalars().all())
    async def get_auto_candidates(
        self,
        session: AsyncSession
    ) -> List[Model]:
        """获取可以参与 auto 选举的候选模型。
        注意：free_tier / oauth 类供应商不需要 ApiKey 表里的密钥，
        只要 enabled + auto_enabled + 未手动排除即可成为候选（否则免费模型永远进不了 auto）。
        v4.0: 服务商被禁用时其模型不参与 auto 选举。"""
        query = (
            select(Model, Provider)
            .join(Provider, Model.provider_id == Provider.id)
            .where(
                Model.enabled == True,
                Model.auto_enabled == True,
                Model.auto_excluded == False,  # v2.0: 排除用户手动排除的
                Provider.enabled == True,      # v4.0: 跳过已禁用的服务商
            )
            .order_by(Model.priority_boost.desc(), Model.is_free.desc(), Model.input_price.asc())
        )
        result = await session.execute(query)
        rows = result.all()
        valid = []
        for model, provider in rows:
            cred = getattr(provider, "credential_type", "api_key")
            if cred in ("free_tier", "oauth"):
                # 免费层 / OAuth 供应商无需 ApiKey 表中的密钥
                valid.append(model)
                continue
            key_result = await session.execute(
                select(ApiKey).where(
                    ApiKey.provider_id == model.provider_id,
                    ApiKey.is_active == True
                )
            )
            if key_result.first() is not None:
                valid.append(model)
        return valid
    async def get_by_id(self, session: AsyncSession, model_id: int) -> Optional[Model]:
        """根据 ID 获取模型"""
        result = await session.execute(select(Model).where(Model.id == model_id))
        return result.scalar_one_or_none()
    async def get_by_full_id(
        self,
        session: AsyncSession,
        provider_name: str,
        model_id: str
    ) -> Optional[Model]:
        """根据 provider/model 获取模型。v4.0: 服务商被禁用时返回 None（视为不可用）。"""
        query = (
            select(Model)
            .join(Provider, Model.provider_id == Provider.id)
            .where(
                Provider.name == provider_name,
                Model.model_id == model_id,
                Provider.enabled == True,  # v4.0: 跳过已禁用的服务商
            )
        )
        result = await session.execute(query)
        return result.scalar_one_or_none()
    async def update_model(
        self,
        session: AsyncSession,
        model_id: int,
        display_name: Optional[str] = None,
        auto_enabled: Optional[bool] = None,
        enabled: Optional[bool] = None,
        input_price: Optional[float] = None,
        output_price: Optional[float] = None,
        cache_read_input_price: Optional[float] = None,
        cache_write_input_price: Optional[float] = None,
        success_rate: Optional[float] = None,
        is_free: Optional[bool] = None,
        priority_boost: Optional[int] = None,
        auto_excluded: Optional[bool] = None,
        supports_reasoning_effort: Optional[bool] = None,
        request_overrides: Optional[dict] = None
    ) -> Optional[Model]:
        """更新模型配置"""
        model = await self.get_by_id(session, model_id)
        if not model:
            return None
        if display_name is not None:
            model.display_name = display_name
        if auto_enabled is not None:
            model.auto_enabled = auto_enabled
        if enabled is not None:
            model.enabled = enabled
        if input_price is not None:
            model.input_price = input_price
        if output_price is not None:
            model.output_price = output_price
        if cache_read_input_price is not None:
            model.cache_read_input_price = cache_read_input_price
        if cache_write_input_price is not None:
            model.cache_write_input_price = cache_write_input_price
        if any(v is not None for v in (input_price, output_price, cache_read_input_price, cache_write_input_price)):
            # 手动改价 → 标记 manual，后续刷新模型不覆盖（想恢复自动价可手动清掉来源）
            model.pricing_source = "manual"
            model.pricing_updated_at = datetime.utcnow()
        if success_rate is not None:
            model.success_rate = success_rate
        if is_free is not None:
            model.is_free = is_free
        if priority_boost is not None:
            model.priority_boost = max(-100, min(100, priority_boost))  # 限制范围
        if auto_excluded is not None:
            model.auto_excluded = auto_excluded
        # The API uses None as "unknown"; explicit True/False is a durable admin override.
        if supports_reasoning_effort is not None:
            model.supports_reasoning_effort = supports_reasoning_effort
        if request_overrides is not None:
            model.request_overrides = request_overrides
        await session.commit()
        await session.refresh(model)
        return model
    async def refresh_models_from_provider(
        self,
        session: AsyncSession,
        provider: Provider,
        key_manager: KeyManager
    ) -> dict:
        """从服务商拉取最新模型列表"""
        # v3.3：base_url 校验，防止空/非法 URL 导致 httpx 报错
        base_url = (provider.base_url or "").strip()
        if not base_url.startswith(("http://", "https://")):
            return {"error": f"Invalid or missing base_url: '{base_url[:80]}'"}
        # free_tier / oauth 无密钥的 provider 也跳过（不需要 key）
        cred_type = getattr(provider, "credential_type", "api_key") or "api_key"
        if cred_type in ("free_tier",):
            # 免费层：尝试通过 free executor 拉取模型（仅 opencode 提供 /models 端点；
            # mimo-free 无端点，保留手动管理）
            from server.core.free_providers import get_free_executor, resolve_free_code
            free_code = resolve_free_code(provider.name, getattr(provider, "oauth_code", None))
            exec_ = get_free_executor(free_code) if free_code else None
            if exec_ is not None:
                try:
                    fetched_ids = await exec_.list_models()
                except Exception as e:
                    logger.warning(f"free_tier list_models failed for {provider.name}: {e}")
                    # 拉取失败：保留已有模型（不报错、不删除），避免误删手动种子模型
                    total_rows = (await session.execute(
                        select(Model).where(Model.provider_id == provider.id)
                    )).scalars().all()
                    return {
                        "added": 0, "updated": 0, "removed": 0,
                        "added_models": [], "removed_models": [],
                        "total": len(list(total_rows)), "pricing_updated": 0,
                        "metric_updated": 0, "pricing_source": None,
                        "pricing_error": f"free_tier fetch failed: {e}",
                    }
                if fetched_ids:
                    added = 0
                    updated = 0
                    added_models = []
                    for mid in fetched_ids:
                        ex = await session.execute(
                            select(Model).where(Model.provider_id == provider.id, Model.model_id == mid)
                        )
                        if ex.scalar_one_or_none() is None:
                            session.add(Model(
                                provider_id=provider.id, model_id=mid, display_name=mid,
                                enabled=True, auto_enabled=False, is_free=False,
                                supports_streaming=True, priority_boost=0, auto_excluded=False,
                                is_manual=False,
                            ))
                            added += 1
                            added_models.append({"model_id": mid, "display_name": mid})
                        else:
                            updated += 1
                    await session.commit()
                    total_rows = (await session.execute(
                        select(Model).where(Model.provider_id == provider.id)
                    )).scalars().all()
                    return {
                        "added": added, "updated": updated, "removed": 0,
                        "added_models": added_models, "removed_models": [],
                        "total": len(list(total_rows)), "pricing_updated": 0,
                        "metric_updated": 0, "pricing_source": None, "pricing_error": None,
                    }
            return {"error": f"Skipping free_tier provider (models managed manually)"}
        # 刷新网络超时（来自 config.yaml model_refresh.timeout_seconds）
        refresh_timeout = get_config().model_refresh.timeout_seconds
        adapter = create_adapter_for_provider(provider.api_type, timeout=refresh_timeout)
        extra_headers = provider.headers if provider.headers else None

        # v3.5：多 key 拉取 —— 该 provider 下每把 active key 都调 list_models
        # 模型存在性取并集；key 归属按各 key 实际返回写 model_api_keys
        key_models: Dict[int, set] = {}            # api_key_id -> {model_id, ...}
        all_model_infos: Dict[str, ModelInfo] = {} # model_id -> ModelInfo（并集，保留首个）
        any_success = False
        atomcode_no_key = (provider.api_type == "atomcode")

        if atomcode_no_key:
            # daemon 完成鉴权，无 AIGate ApiKey；直接以空 key 调一次 list_models
            try:
                _m = await adapter.list_models("", provider.base_url, extra_headers)
                if _m:
                    any_success = True
                    for mi in _m:
                        all_model_infos.setdefault(mi.model_id, mi)
            except Exception as e:
                logger.warning(f"atomcode list_models failed for {provider.name}: {e}")
        else:
            keys = (await session.execute(
                select(ApiKey).where(
                    ApiKey.provider_id == provider.id,
                    ApiKey.is_active == True
                ).order_by(ApiKey.id)
            )).scalars().all()
            if not keys:
                return {"error": "No active API key for this provider"}
            # 多 key 并发 list_models（缩短单服务商刷新耗时；纯网络 IO，无 DB 写）
            async def _fetch_one(k):
                ak = key_manager._crypto.decrypt(k.key_encrypted)
                try:
                    return k.id, await adapter.list_models(ak, provider.base_url, extra_headers)
                except Exception as e:
                    logger.warning(f"list_models failed for key {k.id} of {provider.name}: {e}")
                    return k.id, []
            results = await asyncio.gather(*[_fetch_one(k) for k in keys]) if keys else []
            for kid, _m in results:
                if _m:
                    any_success = True
                    key_models[kid] = {mi.model_id for mi in _m}
                    for mi in _m:
                        all_model_infos.setdefault(mi.model_id, mi)

        models = list(all_model_infos.values())
        list_ok = any_success
        # 获取定价信息（可用于模型名称回退）
        pricing_result = await fetch_provider_pricing(provider.base_url, timeout=refresh_timeout)
        provider_metadata = pricing_result.pricing
        # 如果 list_models 失败或无结果，尝试从 pricing API 提取模型名
        if not models and provider_metadata:
            models = [ModelInfo(
                model_id=name,
                display_name=name,
                is_free=False,
                input_price=0.0,
                output_price=0.0,
                supports_streaming=True,
                context_length=4096
            ) for name in provider_metadata]
            logger.info(f"Fallback: using {len(models)} models from pricing API for {provider.name}")
        added = 0
        updated = 0
        pricing_updated = 0
        metric_updated = 0
        added_models = []
        removed_models = []
        for model_info in models:
            # 价格来源只有两个：服务商自己的 /api/pricing（公益站自定义价）> 内置表；
            # 拿不到就留 0（未知），由用户在管理面板手动填，不用第三方"标准价"猜测
            remote_metadata = match_model_metadata(model_info.model_id, provider_metadata) if provider_metadata else None
            if remote_metadata and "input" in remote_metadata and "output" in remote_metadata:
                pricing = remote_metadata
            else:
                pricing = get_builtin_pricing(model_info.model_id)
            if pricing:
                model_info.input_price = pricing["input"]
                model_info.output_price = pricing["output"]
                model_info.cache_read_input_price = float(pricing.get("cache_read") or 0)
                model_info.cache_write_input_price = float(pricing.get("cache_write") or 0)
                model_info.is_free = pricing["is_free"]
                pricing_updated += 1
            # 移除了 "auto-mark as free" 逻辑：当上游不返回定价时，不再自动标记免费
            # 用户可在管理面板手动设置价格
            # 免费模型不再自动开启 auto（用户反馈不好使），默认 auto_enabled=False，需手动开启
            auto_enabled = False
            existing = await session.execute(
                select(Model).where(
                    Model.provider_id == provider.id,
                    Model.model_id == model_info.model_id
                )
            )
            existing_model = existing.scalar_one_or_none()
            if existing_model:
                existing_model.display_name = model_info.display_name or existing_model.display_name
                # 手动维护的价格（pricing_source == "manual"）不被刷新覆盖
                manual_priced = (existing_model.pricing_source or "").startswith("manual")
                if not manual_priced:
                    existing_model.input_price = model_info.input_price
                    existing_model.output_price = model_info.output_price
                    existing_model.cache_read_input_price = model_info.cache_read_input_price
                    existing_model.cache_write_input_price = model_info.cache_write_input_price
                    existing_model.is_free = model_info.is_free
                existing_model.supports_streaming = model_info.supports_streaming
                existing_model.supports_vision = model_info.supports_vision
                if existing_model.supports_reasoning_effort is None and model_info.supports_reasoning_effort is not None:
                    existing_model.supports_reasoning_effort = model_info.supports_reasoning_effort
                # 窗口只在仍是默认 4096 时补齐，用户手动改过的窗口不动
                if existing_model.context_length == 4096 and model_info.context_length != 4096:
                    existing_model.context_length = model_info.context_length
                if remote_metadata:
                    existing_model.success_rate = remote_metadata.get("success_rate")
                    existing_model.avg_latency_ms = remote_metadata.get("avg_latency_ms")
                    existing_model.avg_ttft_ms = remote_metadata.get("avg_ttft_ms")
                    existing_model.avg_tps = remote_metadata.get("avg_tps")
                    existing_model.pricing_source = pricing_result.source_url
                    existing_model.pricing_updated_at = datetime.utcnow()
                    if remote_metadata.get("success_rate") is not None:
                        metric_updated += 1
                if not manual_priced:
                    if existing_model.input_price == 0 and model_info.input_price > 0:
                        existing_model.input_price = model_info.input_price
                    if existing_model.output_price == 0 and model_info.output_price > 0:
                        existing_model.output_price = model_info.output_price
                    if existing_model.cache_read_input_price == 0 and model_info.cache_read_input_price > 0:
                        existing_model.cache_read_input_price = model_info.cache_read_input_price
                    if existing_model.cache_write_input_price == 0 and model_info.cache_write_input_price > 0:
                        existing_model.cache_write_input_price = model_info.cache_write_input_price
                    if not existing_model.is_free and model_info.is_free:
                        existing_model.is_free = True
                        # 不再自动开启 auto；免费模型需用户手动参与选举
                updated += 1
            else:
                new_model = Model(
                    provider_id=provider.id,
                    model_id=model_info.model_id,
                    display_name=model_info.display_name or model_info.model_id,
                    input_price=model_info.input_price,
                    output_price=model_info.output_price,
                    cache_read_input_price=model_info.cache_read_input_price,
                    cache_write_input_price=model_info.cache_write_input_price,
                    success_rate=remote_metadata.get("success_rate") if remote_metadata else None,
                    avg_latency_ms=remote_metadata.get("avg_latency_ms") if remote_metadata else None,
                    avg_ttft_ms=remote_metadata.get("avg_ttft_ms") if remote_metadata else None,
                    avg_tps=remote_metadata.get("avg_tps") if remote_metadata else None,
                    pricing_source=pricing_result.source_url if remote_metadata else "",
                    pricing_updated_at=datetime.utcnow() if remote_metadata else None,
                    is_free=model_info.is_free,
                    auto_enabled=auto_enabled,
                    enabled=True,
                    supports_streaming=model_info.supports_streaming,
                    supports_vision=model_info.supports_vision,
                    supports_reasoning_effort=model_info.supports_reasoning_effort,
                    context_length=model_info.context_length,
                    priority_boost=0,
                    auto_excluded=False
                )
                session.add(new_model)
                if remote_metadata and remote_metadata.get("success_rate") is not None:
                    metric_updated += 1
                added += 1
                added_models.append({
                    "model_id": model_info.model_id,
                    "display_name": model_info.display_name or model_info.model_id,
                })
        # v3.5：写模型 ↔ key 归属（model_api_keys）
        if not atomcode_no_key and key_models:
            now = datetime.utcnow()
            for api_key_id, mids in key_models.items():
                for mid in mids:
                    mrow = (await session.execute(
                        select(Model).where(Model.provider_id == provider.id, Model.model_id == mid)
                    )).scalar_one_or_none()
                    if not mrow:
                        continue
                    rel = (await session.execute(
                        select(ModelApiKey).where(
                            ModelApiKey.model_id == mrow.id,
                            ModelApiKey.api_key_id == api_key_id
                        )
                    )).scalar_one_or_none()
                    if rel:
                        rel.last_seen_at = now
                    else:
                        session.add(ModelApiKey(model_id=mrow.id, api_key_id=api_key_id, last_seen_at=now))
            # 初始兜底：对 provider 下尚无任何归属记录的 model，归属 provider 全部 active key
            # （老数据 / 手动模型在首次 refresh 后即可轮询，无需等各 key 实际返回）
            existing_rel = (await session.execute(
                select(ModelApiKey.model_id)
                .join(Model, ModelApiKey.model_id == Model.id)
                .where(Model.provider_id == provider.id)
            )).scalars().all()
            existing_rel_set = set(existing_rel)
            all_keys_ids = list(key_models.keys())
            if all_keys_ids:
                all_models = (await session.execute(
                    select(Model).where(Model.provider_id == provider.id)
                )).scalars().all()
                for m in all_models:
                    if m.id not in existing_rel_set:
                        for akid in all_keys_ids:
                            session.add(ModelApiKey(model_id=m.id, api_key_id=akid, last_seen_at=now))
            # 过期清理：last_seen_at 早于阈值（7 天）的归属删除
            expiry = now - timedelta(days=7)
            stale = (await session.execute(
                select(ModelApiKey)
                .join(Model, ModelApiKey.model_id == Model.id)
                .where(Model.provider_id == provider.id, ModelApiKey.last_seen_at < expiry)
            )).scalars().all()
            for r in stale:
                await session.delete(r)

        # 清理已从上游下架、但本地仍存在的自动同步模型（失效模型）
        # 安全约束：仅在 list_models 成功(list_ok)且返回非空(models)时才清理，
        # 避免上游临时故障/超时导致误删该 provider 全部模型；手动添加(is_manual=True)的模型始终保留
        removed = 0
        if get_config().model_refresh.remove_missing_models and list_ok and models:
            fetched_ids = {m.model_id for m in models}
            stale = (await session.execute(
                select(Model).where(
                    Model.provider_id == provider.id,
                    Model.model_id.notin_(fetched_ids),
                    Model.is_manual.is_(False),
                )
            )).scalars().all()
            for m in stale:
                removed_models.append({
                    "model_id": m.model_id,
                    "display_name": m.display_name or m.model_id,
                })
                await session.delete(m)
                removed += 1
            if stale:
                logger.info(f"Removed {removed} stale model(s) for {provider.name} (not in upstream list)")
        await session.commit()
        total = await session.execute(
            select(Model).where(Model.provider_id == provider.id)
        )
        total_count = len(list(total.scalars().all()))
        return {
            "added": added,
            "updated": updated,
            "removed": removed,
            "added_models": added_models,
            "removed_models": removed_models,
            "total": total_count,
            "pricing_updated": pricing_updated,
            "metric_updated": metric_updated,
            "pricing_source": pricing_result.source_url,
            "pricing_error": pricing_result.error,
        }
