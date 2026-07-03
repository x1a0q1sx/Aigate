import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from server.adapters.openai_compat import OpenAICompatAdapter
from server.adapters.xyusec_pricing import (
    _extract_metrics_from_json,
    _extract_pricing_from_json,
    match_model_metadata,
)
from server.api.v1_router import _format_sse_chunk
from server.core.auto_router import AutoRouter, RouteResult
from server.core.health_checker import HealthChecker


def test_openai_adapter_timeout_uses_seconds():
    adapter = OpenAICompatAdapter(timeout=60)

    assert adapter.timeout == 60


def test_format_sse_chunk_outputs_valid_json_and_routed_model():
    payload = _format_sse_chunk({"model": "upstream-model", "choices": []}, "Provider/model-a")
    text = payload.decode("utf-8")

    assert text.startswith("data: ")
    assert text.endswith("\n\n")
    decoded = json.loads(text.removeprefix("data: ").strip())
    assert decoded["model"] == "Provider/model-a"
    assert decoded["choices"] == []


def test_pricing_parser_merges_new_api_shapes():
    pricing = _extract_pricing_from_json({
        "data": {
            "pricing": [
                {"model_name": "openai/gpt-4o-mini", "model_ratio": 0.15, "completion_ratio": 4},
                {"model": "deepseek-chat", "input_price": 0.14, "output_price": 0.28},
            ]
        }
    })
    metrics = _extract_metrics_from_json({
        "data": {
            "models": [
                {"model_name": "openai/gpt-4o-mini", "success_rate": 99.95, "avg_tps": 42.5},
            ]
        }
    })

    assert pricing["openai/gpt-4o-mini"]["input"] == 0.15
    assert pricing["openai/gpt-4o-mini"]["output"] == 0.6
    assert pricing["deepseek-chat"]["output"] == 0.28
    assert metrics["openai/gpt-4o-mini"]["success_rate"] == 99.95
    assert match_model_metadata("gpt-4o-mini", pricing)["input"] == 0.15


def test_health_checker_mark_cooling_sets_future_deadline():
    checker = HealthChecker()
    before = datetime.utcnow()

    checker.mark_cooling(123, seconds=30)

    assert checker.is_cooling(123) is True
    assert checker._cooling[123] > before


@pytest.mark.asyncio
async def test_auto_router_excludes_sticky_model_when_requested():
    sticky_model = SimpleNamespace(
        id=1,
        enabled=True,
        auto_enabled=True,
        auto_excluded=False,
        provider_id=10,
    )
    replacement_model = SimpleNamespace(
        id=2,
        enabled=True,
        auto_enabled=True,
        auto_excluded=False,
        provider_id=10,
        is_free=True,
    )
    provider = SimpleNamespace(id=10, api_type="openai_compat")
    key = SimpleNamespace(id=99, key_encrypted="encrypted")

    class DummyCatalog:
        async def get_by_id(self, session, model_id):
            return sticky_model if model_id == sticky_model.id else None

        async def get_auto_candidates(self, session):
            return [sticky_model, replacement_model]

    class DummyHealthChecker:
        def is_cooling(self, model_id):
            return False

        def get_cached_status(self, model_id):
            return None

    class DummyKeyManager:
        _crypto = SimpleNamespace(decrypt=lambda value: "decrypted-key")

    class DummyRateLimiter:
        async def check_limit(self, session, model_id, key_id):
            return True

    class DummyRankingService:
        async def rank_all(self, session, candidates, providers, cooling):
            return [
                SimpleNamespace(model_id=model.id, excluded_reason=None, final_score=1, has_full_data=True)
                for model in candidates
            ]

    class DummyResult:
        def scalar_one_or_none(self):
            return key

    class DummySession:
        async def get(self, model_cls, provider_id):
            return provider

        async def execute(self, query):
            return DummyResult()

    router = AutoRouter(
        model_catalog=DummyCatalog(),
        health_checker=DummyHealthChecker(),
        key_manager=DummyKeyManager(),
        rate_limiter=DummyRateLimiter(),
    )
    router.ranking_service = DummyRankingService()
    router._set_sticky("conversation", sticky_model.id)

    result = await router.get_best_candidate(
        DummySession(),
        conversation_id="conversation",
        exclude_model_ids={sticky_model.id},
    )

    assert isinstance(result, RouteResult)
    assert result.success is True
    assert result.model.id == replacement_model.id
