"""E1: 模型别名解析（请求名 → 实际路由目标）。

带 15s 内存缓存：别名表很小、变更低频，逐请求查表不必要。
"""
import time
from typing import Optional

from sqlalchemy import select

from ..db import AsyncSessionLocal
from ..models.model_alias import ModelAlias

_cache = {"map": {}, "loaded_at": 0.0}
_TTL = 15.0


async def _load_map() -> dict:
    now = time.monotonic()
    if now - _cache["loaded_at"] < _TTL:
        return _cache["map"]
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(ModelAlias.alias, ModelAlias.target)
                .where(ModelAlias.enabled.is_(True))
            )).all()
        _cache["map"] = {a: t for a, t in rows}
        _cache["loaded_at"] = now
    except Exception:
        pass  # 表尚不存在等情况：沿用旧缓存
    return _cache["map"]


def invalidate_cache() -> None:
    _cache["loaded_at"] = 0.0


async def resolve_alias(name: str) -> Optional[str]:
    """命中别名返回目标名；未命中返回 None。防自环：alias→自身/无 target 不改写。"""
    if not name:
        return None
    m = await _load_map()
    target = m.get(name)
    if target and target != name:
        return target
    return None
