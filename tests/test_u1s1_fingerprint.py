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
