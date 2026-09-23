# -*- coding: utf-8 -*-
"""v4.3 能力感知路由：请求画像、模态硬闸（未知必须放行）、长上下文排序偏好。"""
import asyncio
import pytest

from server.core.context_guard import (
    RequestProfile, analyze_request, media_mismatch_reason,
    capability_preference, LONG_CONTEXT_THRESHOLD_TOKENS,
)


class _Msg:
    def __init__(self, content, **kw):
        self.content = content
        for k, v in kw.items():
            setattr(self, k, v)


class _Req:
    def __init__(self, messages, tools=None):
        self.messages = messages
        self.tools = tools


class _M:
    def __init__(self, **kw):
        self.input_modalities = kw.get("input_modalities")
        self.capability_source = kw.get("capability_source", "")
        self.supports_vision = kw.get("supports_vision", False)
        self.context_length = kw.get("context_length", 0)
        self.observed_context_limit = kw.get("observed_context_limit")
        self.auto_excluded = kw.get("auto_excluded", False)
        self.id = kw.get("id")


def test_analyze_detects_image_and_audio():
    req = _Req([_Msg([{"type": "text", "text": "看图"},
                      {"type": "image_url", "image_url": {"url": "data:..."}}]),
                _Msg([{"type": "input_audio", "input_audio": {"data": "..."}}])])
    p = analyze_request(req)
    assert p.has_image and p.has_audio
    assert not p.is_long


def test_analyze_plain_text_no_media():
    p = analyze_request(_Req([_Msg("hi")]))
    assert not p.has_image and not p.has_audio


def test_analyze_long_request_flag():
    big = "a" * (LONG_CONTEXT_THRESHOLD_TOKENS * 4 + 100)
    p = analyze_request(_Req([_Msg(big)]))
    assert p.is_long


# ── 硬闸安全边界：只有可信来源的闭合模态集合参与拦截 ──

def test_unknown_modality_never_blocked():
    m = _M(input_modalities=None, capability_source="")
    assert media_mismatch_reason(m, RequestProfile(100, has_image=True)) == ""


def test_inferred_capability_not_trusted_for_blocking():
    """名称推断（inferred）不能当闭合集合拦截：audio 推断缺失 ≠ 模型不支持 audio"""
    m = _M(input_modalities=["text", "image"], capability_source="inferred")
    assert media_mismatch_reason(m, RequestProfile(100, has_audio=True)) == ""


def test_known_text_only_blocks_image_request():
    m = _M(input_modalities=["text"], capability_source="openrouter")
    assert "image" in media_mismatch_reason(m, RequestProfile(100, has_image=True))
    # 纯文本请求不受影响
    assert media_mismatch_reason(m, RequestProfile(100)) == ""


def test_known_vision_passes_image_request():
    m = _M(input_modalities=["text", "image"], capability_source="provider")
    assert media_mismatch_reason(m, RequestProfile(100, has_image=True)) == ""


def test_manual_source_trusted():
    m = _M(input_modalities=["text"], capability_source="manual")
    assert media_mismatch_reason(m, RequestProfile(100, has_image=True)) != ""


# ── 排序偏好 ──

def test_preference_image_bonus_for_vision_models():
    prof = RequestProfile(1000, has_image=True)
    vision = _M(input_modalities=["text", "image"], capability_source="openrouter")
    unknown = _M()
    assert capability_preference(vision, prof) > capability_preference(unknown, prof)


def test_preference_long_context_window_tiers():
    prof = RequestProfile(LONG_CONTEXT_THRESHOLD_TOKENS + 1000)
    small = _M(context_length=32768)
    mid = _M(context_length=200000)
    huge = _M(context_length=1048576)
    assert capability_preference(mid, prof) > capability_preference(small, prof)
    assert capability_preference(huge, prof) > capability_preference(mid, prof)


def test_preference_observed_limit_can_downgrade_tier():
    prof = RequestProfile(LONG_CONTEXT_THRESHOLD_TOKENS + 1000)
    m = _M(context_length=1048576, observed_context_limit=100000)
    # 实测窗口 100K 落在 >=32K 档（<128K），不是标称 1M 的顶档
    assert capability_preference(m, prof) == 2.0


def test_preference_zero_on_plain_short_request():
    assert capability_preference(_M(context_length=1048576), RequestProfile(50)) == 0.0


# ── auto 选举过滤：带 profile 的 _filter_candidates ──

def test_auto_filter_skips_known_text_only_for_image_requests():
    from server.core.auto_router import AutoRouter
    ar = AutoRouter()
    ar.health_checker = None
    good = _M(id=1, auto_excluded=False, input_modalities=["text", "image"], capability_source="openrouter")
    bad = _M(id=2, auto_excluded=False, input_modalities=["text"], capability_source="openrouter")
    unknown = _M(id=3, auto_excluded=False)
    kept = ar._filter_candidates([good, bad, unknown], RequestProfile(100, has_image=True))
    assert [m.id for m in kept] == [1, 3]  # bad 被拦，unknown 放行


# ── OpenRouter 模态解析 ──

def test_openrouter_parse_modalities_and_max_out(monkeypatch):
    from server.core import intelligence_sync as isync

    async def fake_get(url, params=None):
        return {"data": [{
            "id": "anthropic/claude-4.5-sonnet",
            "name": "Claude 4.5 Sonnet",
            "context_length": 200000,
            "supported_parameters": ["reasoning_effort"],
            "architecture": {"input_modalities": ["text", "image", "pdf"]},
            "top_provider": {"max_completion_tokens": 64000},
        }]}
    monkeypatch.setattr(isync, "_http_get_json", fake_get)
    out = asyncio.run(isync.fetch_openrouter_models())
    e = out[isync._norm_name("anthropic/claude-4.5-sonnet")]
    assert e["input_modalities"] == ["text", "image", "pdf"]
    assert e["max_output_tokens"] == 64000
    assert e["context_length"] == 200000 and e["supports_reasoning"]


def test_openrouter_single_text_modality_stays_unknown(monkeypatch):
    from server.core import intelligence_sync as isync

    async def fake_get(url, params=None):
        return {"data": [{"id": "x/y", "architecture": {"input_modalities": ["text"]}}]}
    monkeypatch.setattr(isync, "_http_get_json", fake_get)
    out = asyncio.run(isync.fetch_openrouter_models())
    assert out[isync._norm_name("x/y")]["input_modalities"] is None


# ── 名称启发式（仅正向） ──

def test_infer_modalities_positive_only():
    from server.core.model_capabilities import infer_modalities
    assert "image" in infer_modalities("qwen3-vl-32b")
    assert infer_modalities("deepseek-v4-pro") is None  # 无把握 → 未知
