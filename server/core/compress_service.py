
from __future__ import annotations
from typing import Any, Dict, List, Optional

from server.config import get_config
from server.core.token_saver import apply_rtk
from server.core.caveman_saver import apply_caveman
from server.core.ponytail_saver import apply_ponytail


def _to_dict_messages(messages: List[Any]) -> List[Dict[str, Any]]:
    out = []
    for m in messages or []:
        if hasattr(m, "model_dump"):
            out.append(m.model_dump(exclude_none=True))
        elif isinstance(m, dict):
            out.append(dict(m))
        else:
            role = getattr(m, "role", "user")
            content = getattr(m, "content", "")
            out.append({"role": role, "content": content})
    return out


def _chars(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
    return total


def compress_messages(
    messages: List[Any],
    *,
    rtk_enabled: Optional[bool] = None,
    caveman_enabled: Optional[bool] = None,
    ponytail_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Apply AIGate token savers and return compressed messages + comparable stats."""
    cfg = get_config()
    original = _to_dict_messages(messages)
    current = original
    original_chars = _chars(original)
    steps = []

    ts = getattr(cfg, "token_saver", None)
    if rtk_enabled is None:
        rtk_enabled = getattr(ts, "enabled", True) if ts else True
    if rtk_enabled:
        current, st = apply_rtk(current, enabled=True)
        steps.append({
            "id": "rtk",
            "applied": bool(st.get("applied")),
            "rules_hit": st.get("rules_hit", []),
            "chars_saved": int(st.get("chars_saved", 0) or 0),
        })

    extra = getattr(cfg, "token_saver_extra", None)
    if caveman_enabled is None:
        caveman_enabled = bool(getattr(extra, "caveman_enabled", False)) if extra else False
    if caveman_enabled:
        before = _chars(current)
        current, st = apply_caveman(current, enabled=True)
        after = _chars(current)
        steps.append({
            "id": "caveman",
            "applied": bool(st.get("applied")),
            "chars_saved": int(st.get("saved_chars", max(0, before - after)) or 0),
        })

    if ponytail_enabled is None:
        ponytail_enabled = bool(getattr(extra, "ponytail_enabled", False)) if extra else False
    if ponytail_enabled:
        before = _chars(current)
        current, st = apply_ponytail(current, enabled=True)
        after = _chars(current)
        steps.append({
            "id": "ponytail",
            "applied": bool(st.get("applied")),
            "chars_saved": int(st.get("saved_chars", max(0, before - after)) or 0),
        })

    compressed_chars = _chars(current)
    saved = max(0, original_chars - compressed_chars)
    ratio = (saved / original_chars) if original_chars else 0.0
    return {
        "messages": current,
        "original_chars": original_chars,
        "compressed_chars": compressed_chars,
        "chars_saved": saved,
        "savings_ratio": round(ratio, 4),
        "steps": steps,
    }
