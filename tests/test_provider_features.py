from types import SimpleNamespace

from server.api.v1_router import _without_unsupported_reasoning
from server.core.combo_router import pick_next_index
from server.core.model_capabilities import infer_reasoning_effort_support
from server.core.proxy_pool import ProxyPool
from server.schemas.chat import ChatCompletionRequest


def test_reasoning_capability_has_explicit_override_and_safe_inference():
    assert infer_reasoning_effort_support("codex_responses", "any-model") is True
    assert infer_reasoning_effort_support("anthropic", "claude-sonnet-4-6") is True
    assert infer_reasoning_effort_support("anthropic", "claude-3-opus") is False
    assert infer_reasoning_effort_support("openai_compat", "text-embedding-small") is False
    assert infer_reasoning_effort_support("openai_compat", "gpt-5-mini") is True

    request = ChatCompletionRequest(
        model="m", messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="high",
    )
    unsupported = SimpleNamespace(supports_reasoning_effort=False, model_id="m")
    supported = SimpleNamespace(supports_reasoning_effort=True, model_id="m")

    assert _without_unsupported_reasoning(request, unsupported).reasoning_effort is None
    assert _without_unsupported_reasoning(request, supported).reasoning_effort == "high"


def test_combo_round_robin_starts_each_request_at_the_next_target():
    pick_next_index(987001, 3, "fallback")
    assert [pick_next_index(987002, 3, "round_robin") for _ in range(4)] == [0, 1, 2, 0]


def test_provider_proxy_switch_can_force_a_globally_disabled_pool():
    pool = ProxyPool(proxies=[{"url": "http://127.0.0.1:18080"}], enabled=False)

    assert pool.proxied_kwargs() == {}
    assert pool.proxied_kwargs(force=True) == {"proxy": "http://127.0.0.1:18080"}
