"""
RTK Token Saver — 注入式 Prompt 压缩器

设计哲学（来自 9Router RTK）：
  - 在请求到达上游 LLM 之前，对 system / messages 做内容压缩
  - 失败时（规则误判/正则异常）必须安全回退到原文，绝不影响业务正确性
  - 5 个基础规则（默认开启）针对最浪费 Token 的场景：
      1) 删除 system 中的「请你认真...」之类的客套话
      2) 删除 system 中的 JSON 示例 / 多余空白行
      3) 把冗长 security policy 折叠为单行 hint
      4) 合并连续空行
      5) 折叠重复空格

每个规则：
  - 输入：original_text (str)
  - 输出：reduced_text (str)，如果不命中返回原文
  - 必须纯函数（无副作用），异常时返回原文 + log
关闭开关：config.token_saver.enabled = False
"""
from __future__ import annotations
import re
import logging
from typing import List, Dict, Any
logger = logging.getLogger(__name__)

# ── 规则实现 ────────────────────────────────────────────────────────
# 命中：删除常见「礼貌请求词」前缀，减少冗余 Token
_RE_PLEASE_BE = re.compile(
    r"\b(please\s+(be\s+)?(very\s+)?(careful|thorough|meticulous|precise|diligent|strict)\b[^.]*\.\s*)",
    re.IGNORECASE,
)
# 命中：JSON 代码示例（user 给的格式化示例，不是 system 必须项），折叠为占位
_RE_JSON_EXAMPLE = re.compile(
    r"```(?:json|JSON)?\s*\{[\s\S]{200,}?\}\s*```",
    re.MULTILINE,
)
# 命中：长篇 security / safety policy 文段，折叠为单行 hint
_RE_LONG_POLICY = re.compile(
    r"(?im)^.{0,80}\b(security\s*policy|safety\s*guideline|sensitive\s*information)\b[\s\S]{400,}?(\n\.\s|\n$|$)"
)
# 连续 3+ 空行折叠为 1 行
_RE_BLANK_LINES = re.compile(r"\n{3,}")
# 重复空格（行首除外，不破坏缩进）
_RE_MULTI_SPACES = re.compile(r"(?<=\S)[ \t]{2,}")

MIN_LEN_TO_ACT = 80  # 小于 80 字的短 system 不动


def _r_drop_please(text: str) -> str:
    m = _RE_PLEASE_BE.search(text)
    if not m:
        return text
    return text[: m.start()] + text[m.end():]


def _r_fold_json_example(text: str) -> str:
    if "```" not in text:
        return text
    return _RE_JSON_EXAMPLE.sub("[example omitted by AIGate RTK]", text)


def _r_fold_long_policy(text: str) -> str:
    m = _RE_LONG_POLICY.search(text)
    if not m:
        return text
    hint = "[security policy omitted by AIGate RTK - refer to original]"
    return text[: m.start()] + hint + text[m.end():]


def _r_collapse_blank_lines(text: str) -> str:
    return _RE_BLANK_LINES.sub("\n\n", text)


def _r_collapse_spaces(text: str) -> str:
    return _RE_MULTI_SPACES.sub(" ", text)


# ── 规则集合（顺序很重要） ────────────────────────────────────────
# roles: 规则生效的消息角色白名单。
#   - fold_json_example / fold_long_policy 仅对 system 生效：
#     这两条本意是压缩 system 提示词里的「示例/长 policy」，绝不能误删
#     user 消息中的真实业务数据（如 AutoHunter reviewer 传入的 Finding JSON）。
#   - 其余纯文本/空白类规则对 system 与 user 均生效，压缩收益最大且无副作用。
_RULES: List[Dict[str, Any]] = [
    {"id": "fold_json_example", "fn": _r_fold_json_example, "desc": "折叠 JSON 代码示例为占位符", "roles": ("system",)},
    {"id": "fold_long_policy", "fn": _r_fold_long_policy, "desc": "折叠长篇 security policy 文段", "roles": ("system",)},
    {"id": "drop_please_be", "fn": _r_drop_please, "desc": "删除 'please be careful' 类客套话", "roles": ("system", "user")},
    {"id": "collapse_blank_lines", "fn": _r_collapse_blank_lines, "desc": "连续 3+ 空行折叠为 1 行", "roles": ("system", "user")},
    {"id": "collapse_spaces", "fn": _r_collapse_spaces, "desc": "行内重复空格折叠为单空格", "roles": ("system", "user")},
]


def list_rules() -> List[Dict[str, Any]]:
    """Admin 读取可用规则清单"""
    return [{"id": r["id"], "desc": r["desc"], "default_on": True} for r in _RULES]


def apply_rtk(messages: List[Any], enabled: bool = True) -> tuple[List[Any], Dict[str, Any]]:
    """
    对 messages 应用 RTK 压缩。

    返回:
      (reduced_messages, stats)
      stats: {applied: bool, rules_hit: List[str], chars_saved: int, original_chars: int}

    安全保证：
      - 任何规则抛异常 → 跳过该规则，保留当前结果
      - 全部失败 → 返回原 messages（applied: false）
    """
    stats = {"applied": False, "rules_hit": [], "chars_saved": 0, "original_chars": 0}
    if not enabled or not messages:
        return messages, stats

    total_original = 0
    total_reduced = 0
    any_hit = False

    # 浅拷贝 + 仅修改 system 和 user 字符串内容
    new_messages = []
    for m in messages:
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
        content = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if role not in ("system", "user") or not isinstance(content, str):
            new_messages.append(m)
            continue
        original = content
        total_original += len(original)
        if len(original) < MIN_LEN_TO_ACT:
            new_messages.append(m)
            total_reduced += len(original)
            continue
        reduced = original
        for rule in _RULES:
            # 角色白名单：规则只在其声明允许的角色消息上生效，
            # 避免误删 user 消息里的真实业务数据（如 Finding JSON）。
            allowed_roles = rule.get("roles")
            if allowed_roles is not None and role not in allowed_roles:
                continue
            try:
                before = reduced
                reduced = rule["fn"](reduced)
                if reduced != before:
                    stats["rules_hit"].append(rule["id"])
                    any_hit = True
            except Exception as e:
                logger.warning("RTK rule %s raised: %s", rule["id"], e)
                # 回退到 before（保险）
                reduced = before
        total_reduced += len(reduced)
        if reduced != original:
            # 构造新对象（兼容 pydantic & dict）
            if hasattr(m, "model_copy"):
                new_messages.append(m.model_copy(update={"content": reduced}))
            elif isinstance(m, dict):
                new_messages.append({**m, "content": reduced})
            else:
                new_messages.append(m)
        else:
            new_messages.append(m)

    stats["applied"] = any_hit
    stats["chars_saved"] = max(0, total_original - total_reduced)
    stats["original_chars"] = total_original
    stats["compressed_chars"] = total_reduced
    return new_messages, stats
