"""
模型目录服务
管理模型元数据、刷新、auto 候选选择
v2.0: 支持 priority_boost + auto_excluded
"""
import logging
from typing import List, Optional, Dict
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update, delete
from server.models.provider import Provider
from server.models.model import Model
from server.models.api_key import ApiKey
from server.adapters.base_adapter import BaseAdapter, ModelInfo
from server.adapters.openai_compat import OpenAICompatAdapter
from server.adapters.anthropic_adapter import AnthropicAdapter
from server.adapters.github_adapter import GitHubAdapter
from server.adapters.xyusec_pricing import fetch_provider_pricing, match_model_metadata
from .key_manager import KeyManager

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
def create_adapter_for_provider(api_type: str) -> BaseAdapter:
    """根据 api_type 创建适配器"""
    if api_type == "anthropic":
        return AnthropicAdapter()
    elif api_type == "github":
        return GitHubAdapter()
    elif api_type == "openai_compat":
        return OpenAICompatAdapter()
    else:
        return OpenAICompatAdapter()
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
        enabled_only: bool = True
    ) -> List[Model]:
        """列出模型，支持过滤"""
        conditions = []
        if enabled_only:
            conditions.append(Model.enabled == True)
        if provider_id is not None:
            conditions.append(Model.provider_id == provider_id)
        if is_free is not None:
            conditions.append(Model.is_free == is_free)
        if auto_enabled is not None:
            conditions.append(Model.auto_enabled == auto_enabled)
        query = select(Model)
        if conditions:
            query = query.where(and_(*conditions))
        query = query.order_by(Model.provider_id, Model.model_id)
        result = await session.execute(query)
        return list(result.scalars().all())
    async def get_auto_candidates(
        self,
        session: AsyncSession
    ) -> List[Model]:
        """获取可以参与 auto 选举的候选模型"""
        query = (
            select(Model)
            .join(Provider, Model.provider_id == Provider.id)
            .where(
                Model.enabled == True,
                Model.auto_enabled == True,
                Model.auto_excluded == False  # v2.0: 排除用户手动排除的
            )
            .order_by(Model.priority_boost.desc(), Model.is_free.desc(), Model.input_price.asc())
        )
        result = await session.execute(query)
        candidates = list(result.scalars().all())
        valid = []
        for model in candidates:
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
        """根据 provider/model 获取模型"""
        query = (
            select(Model)
            .join(Provider, Model.provider_id == Provider.id)
            .where(Provider.name == provider_name, Model.model_id == model_id)
        )
        result = await session.execute(query)
        return result.scalar_one_or_none()
    async def update_model(
        self,
        session: AsyncSession,
        model_id: int,
        auto_enabled: Optional[bool] = None,
        enabled: Optional[bool] = None,
        input_price: Optional[float] = None,
        output_price: Optional[float] = None,
        success_rate: Optional[float] = None,
        is_free: Optional[bool] = None,
        priority_boost: Optional[int] = None,
        auto_excluded: Optional[bool] = None
    ) -> Optional[Model]:
        """更新模型配置"""
        model = await self.get_by_id(session, model_id)
        if not model:
            return None
        if auto_enabled is not None:
            model.auto_enabled = auto_enabled
        if enabled is not None:
            model.enabled = enabled
        if input_price is not None:
            model.input_price = input_price
        if output_price is not None:
            model.output_price = output_price
        if success_rate is not None:
            model.success_rate = success_rate
        if is_free is not None:
            model.is_free = is_free
        if priority_boost is not None:
            model.priority_boost = max(-100, min(100, priority_boost))  # 限制范围
        if auto_excluded is not None:
            model.auto_excluded = auto_excluded
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
        result = await session.execute(
            select(ApiKey).where(
                ApiKey.provider_id == provider.id,
                ApiKey.is_active == True
            )
        )
        key = result.scalar_one_or_none()
        if not key:
            return {"error": "No active API key for this provider"}
        api_key = key_manager._crypto.decrypt(key.key_encrypted)
        adapter = create_adapter_for_provider(provider.api_type)
        extra_headers = provider.headers if provider.headers else None
        try:
            models = await adapter.list_models(api_key, provider.base_url, extra_headers)
        except Exception as e:
            models = []
            logger.warning(f"list_models failed for {provider.name}: {e}")
        # 获取定价信息（可用于模型名称回退）
        pricing_result = await fetch_provider_pricing(provider.base_url)
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
        for model_info in models:
            remote_metadata = match_model_metadata(model_info.model_id, provider_metadata) if provider_metadata else None
            if remote_metadata and "input" in remote_metadata and "output" in remote_metadata:
                pricing = remote_metadata
            else:
                pricing = get_builtin_pricing(model_info.model_id)
            if pricing:
                model_info.input_price = pricing["input"]
                model_info.output_price = pricing["output"]
                model_info.is_free = pricing["is_free"]
                pricing_updated += 1
            # 移除了 "auto-mark as free" 逻辑：当上游不返回定价时，不再自动标记免费
            # 用户可在管理面板手动设置价格
            # 未匹配内置定价时保留适配器返回值，只有明确免费才 auto_enabled=True
            auto_enabled = model_info.is_free
            existing = await session.execute(
                select(Model).where(
                    Model.provider_id == provider.id,
                    Model.model_id == model_info.model_id
                )
            )
            existing_model = existing.scalar_one_or_none()
            if existing_model:
                existing_model.display_name = model_info.display_name or existing_model.display_name
                existing_model.input_price = model_info.input_price
                existing_model.output_price = model_info.output_price
                existing_model.is_free = model_info.is_free
                existing_model.supports_streaming = model_info.supports_streaming
                existing_model.supports_vision = model_info.supports_vision
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
                if existing_model.input_price == 0 and model_info.input_price > 0:
                    existing_model.input_price = model_info.input_price
                if existing_model.output_price == 0 and model_info.output_price > 0:
                    existing_model.output_price = model_info.output_price
                if not existing_model.is_free and model_info.is_free:
                    existing_model.is_free = True
                    existing_model.auto_enabled = True
                updated += 1
            else:
                new_model = Model(
                    provider_id=provider.id,
                    model_id=model_info.model_id,
                    display_name=model_info.display_name or model_info.model_id,
                    input_price=model_info.input_price,
                    output_price=model_info.output_price,
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
                    context_length=model_info.context_length,
                    priority_boost=0,
                    auto_excluded=False
                )
                session.add(new_model)
                if remote_metadata and remote_metadata.get("success_rate") is not None:
                    metric_updated += 1
                added += 1
        await session.commit()
        total = await session.execute(
            select(Model).where(Model.provider_id == provider.id)
        )
        total_count = len(list(total.scalars().all()))
        return {
            "added": added,
            "updated": updated,
            "total": total_count,
            "pricing_updated": pricing_updated,
            "metric_updated": metric_updated,
            "pricing_source": pricing_result.source_url,
            "pricing_error": pricing_result.error,
        }