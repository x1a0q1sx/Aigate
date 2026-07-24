"""
Ponytail Saver
思路：把长 system policy（用户已自己写过的）的"二次重复段"折叠
典型场景：用户反复写 system prompt「你是...，请遵守...，你必须...」中往往有
多个相似段或反复强调"不要做X、不要X"。Ponytail 折叠规则：
  1) 找到三种内容相同的相邻段落 → 合并去重（保留首段）
  2) 多个 "\n\n" 段落中若差异 < 10% 视为重复
  3) 安全异常回退

默认开关：config.ponytail.enabled = False
"""
from __future__ import annotations
import logging
import re
from typing import List, Tuple, Dict
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

_MIN_PARA_LEN = 80       # 段落过短不压缩
_SIMILARITY_THRESHOLD = 0.90


def _paragraphs(text: str) -> List[str]:
    # 按两个以上换行分段（保留单换行段内）
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _are_similar(a: str, b: str) -> bool:
    if len(a) < _MIN_PARA_LEN or len(b) < _MIN_PARA_LEN:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _SIMILARITY_THRESHOLD


def apply_ponytail(messages: List, enabled: bool = True) -> Tuple[List, Dict]:
    """对 messages 执行 ponytail 段落折叠"""
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
            if not isinstance(content, str) or len(content) < 200:
                new_messages.append(msg)
                continue
            paras = _paragraphs(content)
            if len(paras) <= 1:
                new_messages.append(msg)
                continue
            # 去重相似段落
            kept = [paras[0]]
            for p in paras[1:]:
                if any(_are_similar(p, k) for k in kept):
                    continue
                kept.append(p)
            if len(kept) < len(paras):
                applied += 1
                before_len = len(content)
                after = "\n\n".join(kept)
                saved = before_len - len(after)
                if saved > 0:
                    saved_chars += saved
                    new_messages.append({**msg, "content": after})
                else:
                    new_messages.append(msg)
            else:
                new_messages.append(msg)
        except Exception as e:
            logger.warning("ponytail rule failed: %s", e)
            new_messages.append(msg)
    return new_messages, {"applied": applied, "saved_chars": saved_chars}
