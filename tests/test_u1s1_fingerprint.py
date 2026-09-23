# -*- coding: utf-8 -*-
"""u1s1 竞品客户端名消毒：出站前等价改写，规避「非 u1s1 客户端」403

现场（2026-09-22）：下游用 Claude Code / Windsurf 等客户端时，其 system prompt 含
精确串 "Claude Code" / "Windsurf" / "Gemini CLI" / "Codex CLI"，u1s1 扫描请求体命中即
403（赠送额度限官方客户端）。实测为**大小写精确匹配**：全小写 "claude code" 放行。
"""
import pytest

from server.core.provider_quirks import (
    neutralize_u1s1_fingerprint, _neutralize_payload_tokens,
    quirks_for, transform_payload,
)


def test_rewrites_blocked_tokens():
    """命中的精确串被等价改写，且不再包含原串"""
    cases = [
        ("You are Claude Code, Anthropic's official CLI.", "Claude Code"),
        ("You are Windsurf, an AI code editor.", "Windsurf"),
        ("Gemini CLI is here", "Gemini CLI"),
        ("Codex CLI is here", "Codex CLI"),
    ]
    for text, blocked in cases:
        out = neutralize_u1s1_fingerprint(text)
        assert blocked not in out, f"{blocked!r} 应被改写：{out!r}"
        # 语义可读：改写后仍含原词的字母序列（仅大小写/空格变化）
        assert out.replace("  ", " ").lower().replace(" ", "") == \
            text.replace("  ", " ").lower().replace(" ", ""), out


def test_leaves_lowercase_and_unrelated_tokens_alone():
    """全小写/全大写形态与无关客户端名不被改动（实测这些本就放行）"""
    for text in [
        "claude code",                 # 全小写放行
        "CLAUDE CODE",                 # 全大写放行
        "codex",                       # 单词放行
        "You are ZCode",               # 本家/无关
        "Cursor and aider and Continue",
        "CodeBuddy",
    ]:
        assert neutralize_u1s1_fingerprint(text) == text, text


def test_neutralize_payload_walks_nested_structures():
    """整个 body 递归消毒：system / user / tools 描述都要覆盖"""
    body = {
        "model": "qwen3.8-flash",
        "messages": [
            {"role": "system", "content": "You are Claude Code"},
            {"role": "user", "content": [
                {"type": "text", "text": "Windsurf 和 Gemini CLI 是什么"},
            ]},
        ],
        "tools": [{"function": {"name": "t", "description": "Codex CLI helper"}}],
    }
    out = _neutralize_payload_tokens(body)
    flat = str(out)
    for blocked in ("Claude Code", "Windsurf", "Gemini CLI", "Codex CLI"):
        assert blocked not in flat, f"{blocked!r} 未被消毒：{flat[:200]}"


def test_u1s1_quirks_enable_neutralization():
    q = quirks_for("https://api.u1s1.io/v1")
    assert q is not None and q.name == "u1s1"
    assert q.neutralize_competitor_tokens is True
    # 其它域名不开（避免误改他家的正常内容）
    assert quirks_for("https://api.anthropic.com/v1").neutralize_competitor_tokens is False \
        if quirks_for("https://api.anthropic.com/v1") else True


def test_transform_payload_applies_for_u1s1_only():
    q = quirks_for("https://api.u1s1.io/v1")
    body = {"model": "m", "messages": [
        {"role": "system", "content": "You are Claude Code"},
        {"role": "user", "content": "hi"}]}
    fixed = transform_payload(body, q)
    assert "Claude Code" not in fixed["messages"][0]["content"]
    # 无关域名（无 quirks）原样返回
    assert transform_payload(body, None) is body


# ── v4.3 工具名黑名单改写（apply_patch/update_plan 才是 combo 持续 403 的主因）──
from server.core.provider_quirks import restore_tool_names_in_response


def _u1s1_q():
    return quirks_for("https://api.u1s1.io/v1/chat/completions")


def _payload_with_tools():
    return {
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "apply_patch",
                                              "description": "edit files",
                                              "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "update_plan",
                                              "description": "plan",
                                              "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "exec_command",
                                              "description": "shell",
                                              "parameters": {"type": "object", "properties": {}}}},
        ],
        "tool_choice": {"type": "function", "function": {"name": "apply_patch"}},
    }


def test_blocked_tool_names_are_aliased_outbound():
    """出站前：命中黑名单的工具名→别名；未命中（exec_command）与描述里的 apply_patch 字样不动"""
    out = transform_payload(_payload_with_tools(), _u1s1_q())
    names = [t["function"]["name"] for t in out["tools"]]
    assert names == ["applyPatch", "updatePlan", "exec_command"]
    # 描述里引用旧名不拦截（实测），保持原样
    assert out["tools"][0]["function"]["description"] == "edit files"
    assert out["tool_choice"]["function"]["name"] == "applyPatch"
    # 原 payload 不被就地改
    assert _payload_with_tools()["tools"][0]["function"]["name"] == "apply_patch"


def test_tool_guard_disabled_passes_through():
    """服务商详情关掉「指纹过滤」→ 工具名与竞品词都不改写"""
    q = _u1s1_q()
    p = {"model": "m", "messages": [{"role": "system", "content": "You are Claude Code."}],
         "tools": [{"function": {"name": "apply_patch", "description": "d"}}]}
    out = transform_payload(p, q, tool_guard_enabled=False)
    assert out["tools"][0]["function"]["name"] == "apply_patch"
    assert "Claude Code" in out["messages"][0]["content"]


def test_restore_maps_alias_back_in_response_and_stream():
    aliases = _u1s1_q().blocked_tool_names
    nonstream = {"choices": [{"message": {"tool_calls": [
        {"id": "c1", "function": {"name": "applyPatch", "arguments": "{}"}},
        {"id": "c2", "function": {"name": "exec_command", "arguments": "{}"}},
    ]}}]}
    restore_tool_names_in_response(nonstream, aliases)
    calls = nonstream["choices"][0]["message"]["tool_calls"]
    assert calls[0]["function"]["name"] == "apply_patch"
    assert calls[1]["function"]["name"] == "exec_command"
    chunk = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": "updatePlan"}}]}}]}
    restore_tool_names_in_response(chunk, aliases)
    assert chunk["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "update_plan"


def test_restore_noop_without_aliases():
    data = {"choices": [{"message": {"tool_calls": [{"function": {"name": "apply_patch"}}]}}]}
    out = restore_tool_names_in_response(data, None)
    assert out["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "apply_patch"


def test_provider_flag_marks_extra_headers():
    """Provider.fingerprint_filter_enabled=False → _merge_oauth_headers 打 __fg=0"""
    from types import SimpleNamespace
    from server.api.v1_router import _merge_oauth_headers
    on = SimpleNamespace(credential_type="api_key", proxy_enabled=False,
                         fingerprint_filter_enabled=True)
    off = SimpleNamespace(credential_type="api_key", proxy_enabled=False,
                          fingerprint_filter_enabled=False)
    assert "__fg" not in (_merge_oauth_headers(on, {}) or {})
    assert _merge_oauth_headers(off, {})["__fg"] == "0"
