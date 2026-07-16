"""
Caveman Token Saver
思路：把中文 prompt 注入"原始语"风格压缩 —— 9Router 中只针对部分场景做。
具体规则：
  1) 同义长词 → 短词（词典替换）
  2) 移除"please"、礼貌寒暄词、无信息量修饰
  3) 标准化空白
  4) 数字短语压缩（一二三 → 123）
严格保守、默认关闭，每次走 try-catch 回退。

默认开关：config.caveman.enabled = False
"""
from __future__ import annotations
import logging
import re
from typing import List, Tuple, Dict

logger = logging.getLogger(__name__)

# 同义压缩词典（价值不大但占 token 的常见多字表达）
_DICT: Dict[str, str] = {
    # 礼貌寒暄
    r"请您帮": "帮",
    r"麻烦您": "",
    r"可以的话": "",
    r"如果可以的话": "",
    r"非常感谢": "",
    r"感激不尽": "",
    r"提前谢谢": "",
    r"please\s+help\s+me": "help",
    r"i\s+would\s+appreciate": "",
    r"please": "",
    # 多余修饰
    r"也许可能": "可能",
    r"是否能够": "能否",
    r"是不是可以": "能否",
    r"我想请问一下": "问",
    r"我想问问": "问",
    r"请问一下": "",
    r"麻烦告知": "问",
    r"能不能麻烦": "能否",
    # 冗长度量
    r"非常大": "大",
    r"非常小": "小",
    r"非常快": "快",
    r"非常慢": "慢",
    # 数字短语
    r"一二三四五": "12345",
    r"一二三四": "1234",
    r"一二三": "123",
    r"一二": "12",
}

_COMPILED = [(re.compile(pat, re.IGNORECASE), rep) for pat, rep in _DICT.items()]

_WHITESPACE = re.compile(r"\s+")


def apply_caveman(messages: List, enabled: bool = True) -> Tuple[List, Dict]:
    """
    对 messages 应用 caveman 压缩。
    返回 (new_messages, stats)
    stats: {"applied": int, "saved_chars": int}
    """
    if not enabled or not messages:
        return messages, {"applied": 0, "saved_chars": 0}
    applied = 0
    saved_chars = 0
    new_messages = []
    for msg in messages:
        try:
            if not isinstance(msg, dict):
                new_messages.append(msg)
                continue
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < 60:
                new_messages.append(msg)
                continue
            before = content
            after = content
            for pat, rep in _COMPILED:
                after = pat.sub(rep, after)
            after = _WHITESPACE.sub(" ", after).strip()
            if after != before:
                applied += 1
                saved_chars += max(0, len(before) - len(after))
                new_messages.append({**msg, "content": after})
            else:
                new_messages.append(msg)
        except Exception as e:
            logger.warning("caveman rule failed on msg: %s", e)
            new_messages.append(msg)
    return new_messages, {"applied": applied, "saved_chars": saved_chars}
