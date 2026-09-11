# -*- coding: utf-8 -*-
"""Fusion 策略执行器测试（设计文档 §测试计划）"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from server.schemas.chat import ChatCompletionRequest
import server.core.fusion as fz


def _target(name, provider=None, model_id=None):
    prov = SimpleNamespace(name=provider or name, id=hash(name) % 1000 + 1)
    mdl = SimpleNamespace(model_id=model_id or name, id=hash(name) % 900 + 11,
                          context_length=128000, observed_context_limit=0,
                          request_overrides=None)
    return {"provider": prov, "model": mdl, "full_id": f"{prov.name}/{mdl.model_id}", "weight": None}


def _resp(text):
    return {"id": "x", "object": "chat.completion", "model": "up",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}


def _req(model="combo:f"):
    return ChatCompletionRequest(model=model,
                                 messages=[{"role": "user", "content": "hello"}])


def _run(coro):
    return asyncio.run(coro)


def test_fusion_partial_fail_judge_called():
    targets = [_target("a"), _target("b"), _target("c")]
    calls = []

    async def call_fn(db, provider, model, request):
        calls.append({"model": model.model_id, "n_msgs": len(request.messages)})
        if model.model_id == "c":
            raise RuntimeError("boom")
        return _resp(f"answer-{model.model_id}")

    async def go():
        return await fz.run_fusion(None, targets, _req(), call_fn=call_fn)

    out, meta = _run(go())
    # 3 候选 + 1 judge
    assert len(calls) == 4
    judge_call = calls[-1]
    assert judge_call["n_msgs"] == 2  # 原始 1 条 + 合成指令
    assert meta["fused"] is True
    assert len(meta["sources"]) == 2  # 只带成功的两份进 judge
    assert out["model"] == "combo:f"
    errs = [a for a in meta["attempts"] if not a.get("ok")]
    assert errs and "boom" in errs[0]["error"]


def test_fusion_all_failed_raises_with_attempts():
    targets = [_target("a"), _target("b")]

    async def call_fn(db, provider, model, request):
        raise RuntimeError("nope")

    with pytest.raises(fz.FusionAllFailed) as ei:
        _run(fz.run_fusion(None, targets, _req(), call_fn=call_fn))
    assert len(ei.value.attempts) == 2


def test_fusion_single_success_skips_judge():
    targets = [_target("a"), _target("b")]
    calls = []

    async def call_fn(db, provider, model, request):
        calls.append(model.model_id)
        if model.model_id == "b":
            raise RuntimeError("down")
        return _resp("only one")

    out, meta = _run(fz.run_fusion(None, targets, _req(), call_fn=call_fn))
    assert calls == ["a", "b"]  # 没有第 3 次 judge 调用
    assert meta["fused"] is False
    assert meta["sources"] == ["a/a"]
    assert out["choices"][0]["message"]["content"] == "only one"


def test_fusion_judge_failure_falls_back_to_longest():
    targets = [_target("a"), _target("b")]

    async def call_fn(db, provider, model, request):
        if len(request.messages) > 1:  # judge 请求
            raise RuntimeError("judge timeout")
        return _resp("short" if model.model_id == "a" else "much longer answer")

    out, meta = _run(fz.run_fusion(None, targets, _req(), call_fn=call_fn))
    assert meta["fused"] is False
    fb = out["aigate_fusion"]
    assert fb.get("fallback") and "judge" in fb["fallback"]
    assert out["choices"][0]["message"]["content"] == "much longer answer"


def test_fusion_empty_content_counts_as_failure():
    """空正文候选按失败计；仅剩一个成功时直接返回（不调 judge）。"""
    targets = [_target("a"), _target("b")]
    calls = []

    async def call_fn(db, provider, model, request):
        calls.append((model.model_id, len(request.messages)))
        if model.model_id == "a":
            return _resp("")   # 空正文 → 视为失败
        return _resp("b answer")

    out, meta = _run(fz.run_fusion(None, targets, _req(), call_fn=call_fn))
    assert "a/a" not in meta["sources"]
    assert meta["fused"] is False
    assert len(calls) == 2  # 无第三次 judge 调用
    assert out["choices"][0]["message"]["content"] == "b answer"


def test_fusion_max_targets_cap():
    targets = [_target(f"m{i}") for i in range(10)]
    seen = []

    async def call_fn(db, provider, model, request):
        seen.append((model.model_id, len(request.messages)))
        return _resp(f"ans-{model.model_id}")

    combo = SimpleNamespace(fusion_config={"max_targets": 3})
    out, meta = _run(fz.run_fusion(None, targets, _req(), combo=combo, call_fn=call_fn))
    cand_calls = [m for m, n in seen if n == 1]   # 候选调用（judge 消息更多）
    assert len(cand_calls) == 3  # 只并行前 3 个；judge 从这 3 个里选（第 4 次调用）
    assert len(seen) == 4


def test_fusion_judge_default_best_intelligence():
    targets = [_target("model-a-low"), _target("model-b-high")]
    chosen_judge = {}

    async def call_fn(db, provider, model, request):
        if len(request.messages) > 1:
            chosen_judge["model"] = model.model_id
            return _resp("final")
        return _resp(f"ans-{model.model_id}")

    # 注入智力评分：b-high 更聪明
    fz._intel_cache["rows"] = [("model-a*", 40), ("model-b*", 95)]
    fz._intel_cache["t"] = time.monotonic()
    try:
        out, meta = _run(fz.run_fusion(None, targets, _req(), call_fn=call_fn))
        assert meta["judge"] == ("model-b-high", "model-b-high")
        assert chosen_judge["model"] == "model-b-high"
    finally:
        fz._intel_cache["rows"] = []
        fz._intel_cache["t"] = 0.0


def test_fusion_precheck_skips_candidate():
    targets = [_target("a"), _target("b")]
    calls = []

    async def precheck(p, m):
        return "context window too small" if m.model_id == "a" else None

    async def call_fn(db, provider, model, request):
        calls.append(model.model_id)
        return _resp(f"ans-{model.model_id}")

    out, meta = _run(fz.run_fusion(None, targets, _req(),
                                   precheck=precheck, call_fn=call_fn))
    assert "a" not in calls  # a 被预检跳过，未实际调用
    assert any(not a.get("ok") and "SkipCandidate" in a.get("error", "")
               for a in meta["attempts"])
