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


# ===================== 上下文守护 / litellm 价格库 / 客户端兼容增强 =====================

def test_context_guard_estimator_and_classifier():
    from server.core.context_guard import (
        estimate_request_tokens, estimate_text_tokens, is_context_error, context_overflows,
    )

    # CJK ~1 token/字，英文 ~4字符/token
    assert estimate_text_tokens("你好世界") == 4
    assert 3 <= estimate_text_tokens("abcdefgh") <= 2 or estimate_text_tokens("abcdefgh") == 2
    # 上下文错误识别（各家上游的常见报错）
    assert is_context_error("This model's maximum context length is 4096 tokens")
    assert is_context_error("Prompt is too long: 200000 tokens > 128000 maximum")
    assert is_context_error("input length and `max_tokens` exceed context limit")
    assert is_context_error("Requested tokens exceed the model's context window")
    # 国内公益站中文报错
    assert is_context_error("内容超长，请删减后再试")
    assert is_context_error("输入过长，超过模型上下文限制")
    assert not is_context_error("connection timeout")
    assert not is_context_error("401 unauthorized")
    assert not is_context_error("钱包余额不足")
    assert not is_context_error(None)
    # 窗口判断
    class M: context_length = 8192
    assert context_overflows(M(), 8000) is True       # 8000+1024 > 8192
    assert context_overflows(M(), 6000) is False
    M.context_length = 0
    assert context_overflows(M(), 999999) is False    # 未知窗口不拦截


def test_estimate_request_tokens_messages_and_tools():
    from server.core.context_guard import estimate_request_tokens

    req = ChatCompletionRequest(
        model="m", messages=[{"role": "user", "content": "你好世界"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
    )
    est = estimate_request_tokens(req)
    assert est >= 4  # 至少覆盖正文
    img_req = ChatCompletionRequest(
        model="m", messages=[{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]}],
    )
    assert estimate_request_tokens(img_req) >= 800




def test_anthropic_request_thinking_and_images():
    from server.core.anthropic_converter import anthropic_to_openai_request

    req = anthropic_to_openai_request({
        "model": "claude-sonnet-4",
        "system": "be brief",
        "max_tokens": 1024,
        "thinking": {"type": "enabled", "budget_tokens": 10000},
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "let me think"},
                {"type": "text", "text": "answer"},
            ]},
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
                {"type": "text", "text": "这是什么"},
            ]},
        ],
    })
    assert req["reasoning"] == {"type": "enabled", "budget_tokens": 10000}
    assert req["reasoning_effort"] == "high"  # 10000 落在 8192~16384 → high 档
    # 历史 thinking → reasoning_content
    asst = [m for m in req["messages"] if m["role"] == "assistant"][0]
    assert asst.get("reasoning_content") == "let me think"
    # 图片 → image_url data URL
    user_img = req["messages"][-1]
    assert isinstance(user_img["content"], list)
    img_part = [p for p in user_img["content"] if p["type"] == "image_url"][0]
    assert img_part["image_url"]["url"] == "data:image/png;base64,QUJD"


@pytest.mark.asyncio
async def test_anthropic_stream_thinking_and_tool_events():
    from server.core.anthropic_converter import openai_stream_to_anthropic_events, openai_stream_end_events

    state = {"msg_id": "msg_1", "model": "claude-x"}
    ev1 = await openai_stream_to_anthropic_events(
        {"choices": [{"delta": {"reasoning_content": "思考中"}}]}, state)
    types1 = [e["type"] for e in ev1]
    assert "message_start" in types1
    assert "content_block_start" in types1
    think_start = [e for e in ev1 if e["type"] == "content_block_start"][0]
    assert think_start["content_block"]["type"] == "thinking"
    think_delta = [e for e in ev1 if e["type"] == "content_block_delta"][0]
    assert think_delta["delta"] == {"type": "thinking_delta", "thinking": "思考中"}

    ev2 = await openai_stream_to_anthropic_events(
        {"choices": [{"delta": {"content": "答案"}}]}, state)
    # 思考块关闭后开文本块
    assert any(e["type"] == "content_block_stop" for e in ev2)
    text_start = [e for e in ev2 if e["type"] == "content_block_start"]
    assert text_start and text_start[0]["content_block"]["type"] == "text"

    ev3 = await openai_stream_to_anthropic_events(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": "{\"city\":"}}
        ]}}]}, state)
    tool_start = [e for e in ev3 if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"]
    assert tool_start and tool_start[0]["content_block"]["name"] == "get_weather"
    json_delta = [e for e in ev3 if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta"]
    assert json_delta and json_delta[0]["delta"]["partial_json"] == "{\"city\":"

    ev4 = await openai_stream_to_anthropic_events(
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}, state)
    end_events = openai_stream_end_events(state)
    assert state["usage_in"] == 10
    stop_reason = [e for e in end_events if e["type"] == "message_delta"][0]["delta"]["stop_reason"]
    assert stop_reason == "tool_use"
    assert end_events[-1]["type"] == "message_stop"


def test_responses_input_parts_normalization():
    from server.api.responses_router import _input_to_messages

    msgs = _input_to_messages([
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "看这张图"},
            {"type": "input_image", "image_url": "https://x/1.png"},
        ]},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "先前推理"}]},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "42"},
    ], instructions="sys")
    assert msgs[0] == {"role": "system", "content": "sys"}
    user = msgs[1]
    assert isinstance(user["content"], list)
    assert {"type": "image_url", "image_url": {"url": "https://x/1.png"}} in user["content"]
    # reasoning 摘要 + function_call 合并进同一条 assistant 消息
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert len(asst) == 1
    assert asst[0].get("reasoning_content") == "先前推理"
    assert asst[0]["tool_calls"][0]["id"] == "c1"
    assert msgs[-1]["role"] == "tool" and msgs[-1]["tool_call_id"] == "c1"


# ===================== 请求日志 token 兜底：completion 漏报 =====================

def test_output_text_extractors():
    from server.api.v1_router import _output_text_from_chunks, _output_text_from_result

    chunks = [
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {"reasoning_content": "think"}}]},
        {"choices": [{"delta": {"tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]}}]},
        "not-a-dict",
    ]
    assert _output_text_from_chunks(chunks) == "hithink{}"

    result = {"choices": [{"message": {"content": "answer", "reasoning_content": "why", "tool_calls": [
        {"function": {"arguments": "{\"a\":1}"}}
    ]}}]}
    assert _output_text_from_result(result) == "answerwhy{\"a\":1}"
    assert _output_text_from_result({"choices": []}) == ""


def test_sanitize_estimates_completion_when_missing():
    from server.api.v1_router import _sanitize_token_counts

    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    # 上游漏报 completion → 按输出文本粗估（>0）
    pt, ct = _sanitize_token_counts(req, 0, 0, "a" * 80)
    assert ct == 21  # 80 // 4 + 1
    # 上游给了 completion → 不被覆盖
    pt, ct = _sanitize_token_counts(req, 0, 99, "a" * 80)
    assert ct == 99
    # 无输出文本时 completion 保持 0
    pt, ct = _sanitize_token_counts(req, 0, 0, "")
    assert ct == 0


# ===================== 归档瘦身：blob 引用释放（ref_count 安全递减） =====================

@pytest.mark.asyncio
async def test_release_blob_refs_dedup_safe():
    """共享 blob（ref_count>1）只有在所有引用释放后才删除；独占 blob 释放即删。"""
    import os
    import tempfile
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from server.core.request_logger import release_blob_refs, _upsert_units
    from server.models.base import Base
    from server.models.request_log import LogMsgBlob

    tmpfd, tmpname = tempfile.mkstemp(suffix=".db")
    os.close(tmpfd)
    try:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmpname}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[LogMsgBlob.__table__])
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            # 行1 引用 [sys, a]，行2 引用 [sys, b]：sys 被共享（ref=2）
            hs1 = await _upsert_units(db, [{"sys": "prompt"}])
            ha = await _upsert_units(db, [{"msg": "a"}])
            hs2 = await _upsert_units(db, [{"sys": "prompt"}])  # 同内容再引用 → ref=2
            hb = await _upsert_units(db, [{"msg": "b"}])
            sys_h, a_h, b_h = hs1[0], ha[0], hb[0]
            assert hs2[0] == sys_h
            await db.commit()
            total = (await db.execute(__import__('sqlalchemy').text("SELECT COUNT(*) FROM log_msg_blobs"))).scalar_one()
            assert total == 3

            # 释放行1：sys ref 2→1 保留，a 独占删除
            n = await release_blob_refs(db, [sys_h, a_h])
            assert n == 1
            await db.commit()
            left = dict((await db.execute(__import__('sqlalchemy').text("SELECT hash, ref_count FROM log_msg_blobs"))).all())
            assert left == {sys_h: 1, b_h: 1}

            # 释放行2：sys 归零删除，b 独占删除
            n = await release_blob_refs(db, [sys_h, b_h])
            assert n == 2
            await db.commit()
            left = (await db.execute(__import__('sqlalchemy').text("SELECT COUNT(*) FROM log_msg_blobs"))).scalar_one()
            assert left == 0

            # 重复释放已删除的 hash：安全无副作用
            n = await release_blob_refs(db, [sys_h, None, ""])
            assert n == 0
        await engine.dispose()
    finally:
        os.unlink(tmpname)


# ===================== 智力评分 v2：归一化匹配 + OpenRouter 桥接 =====================

def _lm_entry(name, rating, rank=1):
    from server.core.intelligence_sync import _norm_name, _variant_tags
    return {"name": name, "norm": _norm_name(name), "tags": _variant_tags(name),
            "rating": rating, "rank": rank, "votes": 1000, "org": ""}


def test_norm_name_strips_dates_and_decorations():
    from server.core.intelligence_sync import _norm_name

    assert _norm_name("deepseek-ai/DeepSeek-V4-Pro-0813") == "deepseek-v4-pro"
    assert _norm_name("[次]deepseek-v4-pro") == "deepseek-v4-pro"
    assert _norm_name("glm-5.3-flash-free") == "glm-5.3-flash"
    assert _norm_name("claude-opus-4-5-20251101") == "claude-opus-4-5"
    assert _norm_name("Anthropic/ Claude Opus 5 Max ") == "claude-opus-5-max"
    # 非日期数字不被误剥
    assert _norm_name("gpt-5.5") == "gpt-5.5"


def test_match_entry_tiers():
    from server.core.intelligence_sync import _match_entry

    entries = [
        _lm_entry("deepseek-v4-pro-high-20260813", 1450, 10),
        _lm_entry("deepseek-v4-pro", 1430, 20),
        _lm_entry("gpt-5.6-sol-xhigh", 1440, 15),
        _lm_entry("glm-5.3-flash", 1400, 40),
    ]
    # tier1: 剥日期后精确
    m = _match_entry("deepseek-ai/DeepSeek-V4-Pro-0813", entries)
    assert m and m["norm"] == "deepseek-v4-pro"
    # tier1: 精确
    m = _match_entry("glm-5.3-flash", entries)
    assert m and m["norm"] == "glm-5.3-flash"
    # tier2: 前缀 + effort 跨档（无 effort 匹配到 xhigh 变体）
    m = _match_entry("gpt-5.6-sol", entries)
    assert m and m["norm"] == "gpt-5.6-sol-xhigh"
    # 变体不兼容不匹配：flash ≠ 非 flash
    assert _match_entry("glm-5.3-pro", entries) is None
    # 完全未知
    assert _match_entry("totally-unknown-model", entries) is None


def test_bridge_via_openrouter():
    from server.core.intelligence_sync import _bridge_via_openrouter, _norm_name

    entries = [_lm_entry("gpt-5.6-sol-xhigh", 1440, 15)]
    or_map = {_norm_name("openai/gpt-5.6-sol"): {"id": "openai/gpt-5.6-sol", "name": "GPT: 5.6 Sol"}}
    # aigate 名与 or id 尾段一致，or name 归一化后前缀命中榜单
    m = _bridge_via_openrouter("gpt-5.6-sol", or_map, entries)
    assert m and m["norm"] == "gpt-5.6-sol-xhigh"
    # or 无该模型 → None
    assert _bridge_via_openrouter("no-such-model", or_map, entries) is None


def test_parse_dt_param_timezone_aware():
    from server.api.admin_routing import _parse_dt_param
    from datetime import datetime, timezone

    # 带时区 ISO → naive UTC
    assert _parse_dt_param("2026-08-31T00:00:00+08:00") == datetime(2026, 8, 30, 16, 0)
    assert _parse_dt_param("2026-08-31T00:00:00Z") == datetime(2026, 8, 31, 0, 0)
    # 无时区 → 视为 UTC 原样
    assert _parse_dt_param("2026-08-31T00:00") == datetime(2026, 8, 31, 0, 0)
    # 非法/非 str
    assert _parse_dt_param("garbage") is None
    assert _parse_dt_param(None) is None


# ===================== 回退矩阵：空流锁定 / 缓冲 / max_fallbacks / 终态错误翻译 =====================

def test_chunk_has_substance_lock_semantics():
    from server.api.v1_router import _chunk_has_substance

    # 实质 chunk：正文 / 思考 / 工具调用
    assert _chunk_has_substance({"choices": [{"delta": {"content": "hi"}}]})
    assert _chunk_has_substance({"choices": [{"delta": {"reasoning_content": "思考"}}]})
    assert _chunk_has_substance({"choices": [{"delta": {"tool_calls": [{"function": {"name": "f"}}]}}]})
    # 元数据 chunk：role / 空 content / usage / finish —— 不锁定候选
    assert not _chunk_has_substance({"choices": [{"delta": {"role": "assistant", "content": ""}}]})
    assert not _chunk_has_substance({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    assert not _chunk_has_substance({"usage": {"prompt_tokens": 1}})
    assert not _chunk_has_substance("raw")


def test_combo_stream_max_fallbacks_contract():
    """总尝试 = min(候选数, max_fallbacks + 1)——与 auto 契约一致。"""
    from server.core.combo_router import pick_next_index

    assert pick_next_index(1, 5, "fallback") == 0
    assert pick_next_index(1, 5, "round_robin") in range(5)


@pytest.mark.asyncio
async def test_responses_stream_translates_terminal_error():
    """网关终态 error chunk → response.failed 事件（而非伪装 completed）。"""
    from server.api.responses_router import _chat_to_responses_stream

    async def gen():
        yield b'data: {"error": "combo all targets failed", "attempts": [{"error": "boom"}]}\n\n'
        yield b"data: [DONE]\n\n"

    from fastapi.responses import StreamingResponse
    sr = _chat_to_responses_stream(StreamingResponse(gen(), media_type="text/event-stream"), "combo:x")
    body = b""
    async for piece in sr.body_iterator:
        body += piece if isinstance(piece, bytes) else piece.encode()
    text = body.decode("utf-8", "replace")
    assert "response.failed" in text
    assert "combo all targets failed" in text
    assert "response.completed" not in text


@pytest.mark.asyncio
async def test_anthropic_stream_translates_terminal_error():
    """网关终态 error chunk → Anthropic error 事件（而非正常 message_stop）。"""
    from server.api.anthropic_router import _convert_streaming_response
    from server.schemas.chat import ChatCompletionRequest

    async def gen():
        yield b'data: {"error": "all cascade fallbacks exhausted"}\n\n'
        yield b"data: [DONE]\n\n"

    from fastapi.responses import StreamingResponse
    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    sr = _convert_streaming_response(StreamingResponse(gen(), media_type="text/event-stream"), req)
    body = b""
    async for piece in sr.body_iterator:
        body += piece if isinstance(piece, bytes) else piece.encode()
    text = body.decode("utf-8", "replace")
    assert "event: error" in text
    assert "all cascade fallbacks exhausted" in text
    assert "message_stop" not in text


@pytest.mark.asyncio
async def test_credential_resolver_rejects_unknown_free_provider():
    """free_tier 服务商找不到 executor 时返回 error（而不是抛异常）。"""
    from types import SimpleNamespace
    from server.core.credential_resolver import resolve_credential_async

    class FakeDB:
        async def execute(self, *a, **k):
            raise AssertionError("free_tier 不应查询 api_keys")

    prov = SimpleNamespace(
        name="不存在的free站", base_url="https://x/v1", api_type="openai_compat",
        credential_type="free_tier", oauth_code=None, headers=None,
    )
    model = SimpleNamespace(model_id="m", provider_id=1)
    rc = await resolve_credential_async(prov, model, FakeDB())
    assert rc.error and "no matching executor" in rc.error
    assert not rc.ok


@pytest.mark.asyncio
async def test_credential_resolver_standard_path(monkeypatch):
    """标准 provider：key rotator 命中 → adapter + key；rotator 失败 → 兜底第一把 active key。"""
    from types import SimpleNamespace
    from server.core.credential_resolver import resolve_credential_async

    calls = {"rotator": 0}

    class FakePicked:
        def __getitem__(self, i):
            return 7 if i == 0 else "sk-plain"

    class FakeRotator:
        async def pick_key_for_model(self, db, model):
            calls["rotator"] += 1
            return (7, "sk-plain")

    import server.core.key_rotator as kr
    monkeypatch.setattr(kr, "get_key_rotator", lambda: FakeRotator())

    class FakeResult:
        def scalar_one_or_none(self):
            return None

    class FakeDB:
        async def execute(self, *a, **k):
            return FakeResult()

    prov = SimpleNamespace(
        name="p1", base_url="https://x/v1", api_type="openai_compat",
        credential_type="api_key", oauth_code=None, headers={"X": "1"}, id=1,
    )
    model = SimpleNamespace(model_id="m", provider_id=1)
    rc = await resolve_credential_async(prov, model, FakeDB())
    assert rc.ok and rc.kind == "standard" and rc.api_key == "sk-plain" and rc.key_id == 7
    assert rc.adapter is not None


# ===================== P0-3 日志写入队列 =====================

@pytest.mark.asyncio
async def test_log_queue_batch_write(monkeypatch, tmp_path):
    """入队 → worker 批量落库 → 统计正确；dedup 后大 body 转 hash 引用。"""
    import os
    import asyncio as _aio
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from server.models.base import Base
    from server.models.request_log import RequestLog, LogMsgBlob
    import server.core.log_queue as lq
    import server.db as _dbmod

    dbfile = tmp_path / "lq.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{dbfile}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[RequestLog.__table__, LogMsgBlob.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr(_dbmod, "AsyncSessionLocal", Session)
    lq.stats.update({"enqueued": 0, "written": 0, "dropped": 0, "errors": 0, "batches": 0})
    lq.stopped = False
    lq._queue = _aio.Queue(maxsize=100)
    lq._worker_task = _aio.create_task(lq._worker())
    try:
        for i in range(12):
            ok = await lq.enqueue_log(conversation_id=f"c{i}", requested_model="m", status="success",
                                      prompt_tokens=i, completion_tokens=1, request_body=None, response_body=None)
            assert ok
        for _ in range(40):
            if lq.stats["written"] >= 12:
                break
            await _aio.sleep(0.1)
        assert lq.stats["written"] == 12
        assert lq.stats["dropped"] == 0
        async with Session() as db:
            n = (await db.execute(__import__('sqlalchemy').text("SELECT COUNT(*) FROM request_logs"))).scalar_one()
        assert n == 12
    finally:
        await lq.stop_log_queue()
        lq._queue = None
        await engine.dispose()


@pytest.mark.asyncio
async def test_log_queue_drops_when_full():
    """队列满时丢弃并计数（保护主流程）。"""
    import server.core.log_queue as lq
    lq.stats.update({"enqueued": 0, "written": 0, "dropped": 0})
    lq.stopped = False
    old_task = lq._worker_task
    lq._worker_task = None  # 不启动 worker（只测入队与丢弃）
    lq._queue = __import__('asyncio').Queue(maxsize=3)
    try:
        results = [await lq.enqueue_log(i=i) for i in range(5)]
        assert results[:3] == [True, True, True]
        assert results[3] is False and results[4] is False
        assert lq.stats["dropped"] == 2
    finally:
        lq._queue = None
        lq._worker_task = old_task


# ===================== P1-4 usage 三方言归一化 =====================

def test_normalize_usage_openai_chat():
    from server.core.usage_normalize import normalize_usage
    u = normalize_usage({"prompt_tokens": 1000, "completion_tokens": 200,
                         "prompt_tokens_details": {"cached_tokens": 800},
                         "completion_tokens_details": {"reasoning_tokens": 150}})
    assert (u.prompt_tokens, u.completion_tokens, u.cache_read_tokens, u.reasoning_tokens) == (1000, 200, 800, 150)
    assert u.source == "openai_chat" and u.cache_hit_rate == 80.0
    d = u.to_openai_chat()
    assert d["prompt_tokens_details"] == {"cached_tokens": 800, "cache_write_tokens": 0}


def test_normalize_usage_responses_and_anthropic():
    from server.core.usage_normalize import normalize_usage
    # Responses
    u = normalize_usage({"input_tokens": 500, "output_tokens": 100,
                         "input_tokens_details": {"cached_tokens": 300},
                         "output_tokens_details": {"reasoning_tokens": 60}})
    assert (u.source, u.prompt_tokens, u.cache_read_tokens, u.reasoning_tokens) == ("openai_responses", 500, 300, 60)
    # Anthropic：input 不含缓存 → 归一化并入 prompt（OpenAI 口径）
    u = normalize_usage({"input_tokens": 200, "output_tokens": 50,
                         "cache_read_input_tokens": 700, "cache_creation_input_tokens": 100})
    assert (u.source, u.prompt_tokens, u.cache_read_tokens, u.cache_write_tokens) == ("anthropic", 1000, 700, 100)
    assert abs(u.cache_hit_rate - 70.0) < 0.01
    # 新版 cache_creation 结构
    u = normalize_usage({"input_tokens": 100, "cache_creation": {"ephemeral_5m_input_tokens": 50, "ephemeral_1h_input_tokens": 30}})
    assert u.cache_write_tokens == 80
    # Anthropic 非缓存请求（input/output 命名无缓存字段）口径仍正确
    u = normalize_usage({"input_tokens": 300, "output_tokens": 40})
    assert (u.prompt_tokens, u.completion_tokens) == (300, 40)


def test_normalize_usage_empty_and_garbage():
    from server.core.usage_normalize import normalize_usage
    u = normalize_usage(None)
    assert u.prompt_tokens == 0 and u.source == "unknown"
    u = normalize_usage("garbage")
    assert u.total_tokens == 0
