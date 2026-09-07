"""A4: 可选响应缓存。

- 指纹 = 模型名 + 请求体全量参数（排除 stream 后按非流式缓存）
- 命中 → 直接回 JSONResponse，不走路由/上游（省 token 与站点额度）
- LRU + TTL 双淘汰；进程内存储（重启即清，可接受）
- 只缓存 200 的非流式响应，超大的不缓存
"""
import hashlib
import json
import time
from collections import OrderedDict
from typing import Optional

from ..config import get_config


class ResponseCache:
    def __init__(self):
        self._store: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
        self.stats = {"hits": 0, "misses": 0, "stores": 0, "evicted": 0}

    def _enabled(self) -> bool:
        return bool(getattr(get_config(), "response_cache", None)
                    and get_config().response_cache.enabled)

    def _key(self, request) -> Optional[str]:
        if not self._enabled():
            return None
        if getattr(request, "stream", False):
            return None  # 流式不缓存
        try:
            raw = request.model_dump()
        except Exception:
            return None
        raw.pop("stream", None)
        try:
            blob = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            return None
        return str(hashlib.sha256(blob.encode("utf-8")).hexdigest())

    def get(self, request) -> Optional[dict]:
        key = self._key(request)
        if not key:
            return None
        entry = self._store.get(key)
        if not entry:
            self.stats["misses"] += 1
            return None
        expires, body = entry
        if time.monotonic() > expires:
            self._store.pop(key, None)
            self.stats["misses"] += 1
            return None
        self._store.move_to_end(key)
        self.stats["hits"] += 1
        return body

    def put(self, request, body: dict) -> None:
        key = self._key(request)
        if not key or not isinstance(body, dict):
            return
        cfg = get_config().response_cache
        try:
            if len(json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")) > cfg.max_body_bytes:
                return
        except Exception:
            return
        self._store[key] = (time.monotonic() + max(1, cfg.ttl_seconds), body)
        self._store.move_to_end(key)
        self.stats["stores"] += 1
        while len(self._store) > max(1, cfg.max_items):
            self._store.popitem(last=False)
            self.stats["evicted"] += 1

    def clear(self) -> int:
        n = len(self._store)
        self._store.clear()
        return n

    def info(self) -> dict:
        cfg = getattr(get_config(), "response_cache", None)
        return {
            "enabled": bool(cfg and cfg.enabled),
            "ttl_seconds": cfg.ttl_seconds if cfg else 0,
            "size": len(self._store),
            **self.stats,
        }


response_cache = ResponseCache()
