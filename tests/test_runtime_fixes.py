import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from server.adapters.openai_compat import OpenAICompatAdapter
from server.adapters.codex_responses import CodexResponsesAdapter
from server.adapters.xyusec_pricing import (
    _extract_metrics_from_json,
    _extract_pricing_from_json,
    match_model_metadata,
)
from server.api.v1_router import _format_sse_chunk, _stream_usage_dict
from server.core.auto_router import AutoRouter, RouteResult
from server.core.health_checker import HealthChecker
from server.core.model_catalog import create_adapter_for_provider
from server.schemas.chat import ChatCompletionRequest


def test_openai_adapter_timeout_uses_seconds():
    adapter = OpenAICompatAdapter(timeout=60)

    assert adapter.timeout == 60


def test_stream_usage_ignores_malformed_upstream_usage():
    assert _stream_usage_dict({"usage": {"prompt_tokens": 3}}) == {"prompt_tokens": 3}
    assert _stream_usage_dict({"usage": "unknown"}) == {}
    assert _stream_usage_dict({"usage": [1, 2]}) == {}
    assert _stream_usage_dict(None) == {}


def test_codex_responses_adapter_builds_responses_payload():
    adapter = CodexResponsesAdapter(timeout=60)
    request = ChatCompletionRequest(
        model="gpt-5.5",
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "ping"},
        ],
        stream=False,
        max_tokens=1,
    )

    payload = adapter._build_responses_payload(request)

    assert payload["model"] == "gpt-5.5"
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["instructions"] == "Be concise."
    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "ping"}],
        }
    ]
    assert payload["reasoning"]["effort"] == "low"
    assert payload["include"] == ["reasoning.encrypted_content"]


def test_codex_responses_adapter_url_shapes():
    adapter = CodexResponsesAdapter()

    assert adapter._build_url("http://127.0.0.1:20128/v1") == "http://127.0.0.1:20128/v1/responses"
    assert adapter._build_url("https://chatgpt.com/backend-api/codex/responses") == "https://chatgpt.com/backend-api/codex/responses"
    assert adapter._build_url("https://example.com/v1/chat/completions") == "https://example.com/v1/responses"


def test_model_catalog_creates_codex_responses_adapter():
    adapter = create_adapter_for_provider("codex_responses")

    assert isinstance(adapter, CodexResponsesAdapter)


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


# ===================== 思考强度：入口透传 + 模型名后缀 + anthropic 映射 =====================

def test_split_effort_suffix():
    from server.api.v1_router import _split_effort_suffix

    assert _split_effort_suffix("combo:my-fast-high") == ("combo:my-fast", "high")
    assert _split_effort_suffix("combo:deep-xhigh") == ("combo:deep", "xhigh")
    assert _split_effort_suffix("prov/gpt-5.2-low") == ("prov/gpt-5.2", "low")
    assert _split_effort_suffix("prov/m-none") == ("prov/m", "none")
    # 无后缀 / 名字本身就是后缀 / 空值
    assert _split_effort_suffix("combo:normal") == ("combo:normal", None)
    assert _split_effort_suffix("-high") == ("-high", None)
    assert _split_effort_suffix("") == ("", None)
    # -xhigh 不应被 -high 规则误剥
    base, effort = _split_effort_suffix("combo:x-xhigh")
    assert effort == "xhigh" and base == "combo:x"


def test_responses_entry_extracts_reasoning_effort():
    from server.api.responses_router import _responses_to_chat_request

    body = {
        "model": "combo:my-fast",
        "input": "hi",
        "reasoning": {"effort": "high", "summary": "auto"},
    }
    req = _responses_to_chat_request(body)
    assert req.reasoning_effort == "high"

    # 无 reasoning / 结构异常时不应设置
    req2 = _responses_to_chat_request({"model": "m", "input": "hi"})
    assert req2.reasoning_effort is None
    req3 = _responses_to_chat_request({"model": "m", "input": "hi", "reasoning": "bad"})
    assert req3.reasoning_effort is None


def test_anthropic_effort_maps_to_thinking_budget():
    from server.adapters.anthropic_adapter import AnthropicAdapter

    adapter = AnthropicAdapter()
    req = ChatCompletionRequest(
        model="claude-x", messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="high",
    )
    payload = adapter._build_payload(req)
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    # 客户端没限输出 → max_tokens 被放大以容纳预算
    assert payload["max_tokens"] == 16384 + 8192
    # thinking 模式下不显式传 temperature
    assert "temperature" not in payload

    # 客户端上限较小时预算收缩
    req2 = ChatCompletionRequest(
        model="claude-x", messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="high", max_tokens=4096,
    )
    payload2 = adapter._build_payload(req2)
    assert payload2["thinking"] == {"type": "enabled", "budget_tokens": 3072}
    assert payload2["max_tokens"] == 4096

    # none/minimal/未知档位不开启思考
    for level in ("none", "minimal", "bogus", None):
        req3 = ChatCompletionRequest(
            model="claude-x", messages=[{"role": "user", "content": "hi"}],
            reasoning_effort=level,
        )
        assert "thinking" not in adapter._build_payload(req3)

    # Anthropic 原生格式（reasoning.type）优先，不被 effort 覆盖
    req4 = ChatCompletionRequest(
        model="claude-x", messages=[{"role": "user", "content": "hi"}],
        reasoning={"type": "enabled", "budget_tokens": 512},
        reasoning_effort="high",
    )
    payload4 = adapter._build_payload(req4)
    assert payload4["thinking"] == {"type": "enabled", "budget_tokens": 512}


@pytest.mark.asyncio
async def test_model_name_resolves_queries():
    """_model_name_resolves 对 combo / provider 模型 / 未知名称的判定。"""
    from server.api.v1_router import _model_name_resolves

    class Combo:
        pass

    class Q:
        def __init__(self, val):
            self.val = val

        def first(self):
            return self.val

        def scalar_one_or_none(self):
            return self.val

    class Sess:
        def __init__(self, results):
            self.results = list(results)
            self.calls = 0

        async def execute(self, q):
            r = self.results[min(self.calls, len(self.results) - 1)]
            self.calls += 1
            return Q(r)

    # combo:xxx 且能查到 → True
    assert await _model_name_resolves(Sess([Combo()]), "combo:xxx") is True
    # combo:xxx 查不到 → False（后缀剥离预检会再试剥后缀基名）
    assert await _model_name_resolves(Sess([None]), "combo:xxx-high") is False
    # provider/model_id 命中
    assert await _model_name_resolves(Sess([(1,)]), "prov/m") is True
    assert await _model_name_resolves(Sess([None]), "prov/m") is False
    # 裸 model_id 命中
    assert await _model_name_resolves(Sess([(1,)]), "gpt-5.2") is True
