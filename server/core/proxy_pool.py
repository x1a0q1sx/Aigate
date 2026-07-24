"""
HTTP 代理池
设计参考 9Router 代理池：
  - 三种策略：round_robin / weighted / random
  - 健康检查熔断（连续 N 次失败放冷却池）
  - httpx proxies 参数格式注入到 adapter 创建 httpx.AsyncClient 时使用
数据源：config.yaml → proxy_pool: { enabled, proxies: [...], strategy }

proxies 元素示例：
    {
      "url": "http://user:pass@host:port",
      "weight": 3,                  # weighted 策略生效
      "name": "us-1"
    }
"""
from __future__ import annotations
import random
import time
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import threading
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# 连续 3 次失败进入熔断冷却 30 秒
_FAIL_THRESHOLD = 3
_COOLDOWN_SECONDS = 30

# 记录「当前请求实际用于线请求的代理 URL」。
# 由 adapter 在创建 httpx 客户端前 set，由请求日志写入处读取；
# 每个请求在独立 asyncio 任务中处理，ContextVar 天然隔离并发，不会串号。
CURRENT_PROXY_URL = ContextVar("current_proxy_url", default=None)


class ProxyPool:
    """HTTP 代理池 + 熔断"""

    def __init__(self, proxies: List[Dict] = None, strategy: str = "round_robin", enabled: bool = False):
        self.enabled = bool(enabled)
        self.strategy = strategy
        # 标准化每个代理为 dict：{name, url, weight}
        self._proxies: List[Dict] = []
        for p in proxies or []:
            if isinstance(p, str):
                self._proxies.append({"name": p, "url": p, "weight": 1})
            else:
                self._proxies.append({
                    "name": p.get("name") or p.get("url", ""),
                    "url": p.get("url", ""),
                    "weight": int(p.get("weight", 1)),
                })
        # 运行时状态
        self._cursor = 0
        self._fail_count: Dict[int, int] = {}        # index → 连续失败次数
        self._cooldown_until: Dict[int, datetime] = {}  # index → 恢复时间
        self._lock = threading.Lock()               # 保护 cursor

    def next_proxy(self) -> Optional[str]:
        """挑一个可用代理 URL，无可用时返回 None（等价于不走代理）"""
        if not self.enabled or not self._proxies:
            return None
        with self._lock:
            avail = [
                (i, p) for i, p in enumerate(self._proxies)
                if self._is_available(i)
            ]
            if not avail:
                logger.warning("[ProxyPool] all proxies in cooldown, fallback to direct")
                return None
            if self.strategy == "weighted":
                weights = [max(1, p.get("weight", 1)) for _, p in avail]
                chosen = random.choices(avail, weights=weights, k=1)[0]
            elif self.strategy == "random":
                chosen = random.choice(avail)
            else:
                # round_robin（默认）
                idx = self._cursor % len(avail)
                chosen = avail[idx]
                self._cursor = (idx + 1) % len(avail)
            return chosen[1]["url"]

    def proxied_kwargs(self) -> dict:
        """返回传给 httpx.AsyncClient 的 proxies 参数，代理池关闭时返回空 dict"""
        if not self.enabled:
            return {}
        proxy = self.next_proxy()
        if not proxy:
            return {}
        return {"proxy": proxy}

    def _is_available(self, idx: int) -> bool:
        cd = self._cooldown_until.get(idx)
        if cd and datetime.utcnow() < cd:
            return False
        if cd and datetime.utcnow() >= cd:
            self._cooldown_until.pop(idx, None)
            self._fail_count.pop(idx, None)
        return True

    def mark_success(self, proxy_index: Optional[int] = None):
        if proxy_index is None:
            return
        self._fail_count.pop(proxy_index, None)
        self._cooldown_until.pop(proxy_index, None)

    def mark_failure(self, proxy_index: Optional[int]):
        if proxy_index is None:
            return
        self._fail_count[proxy_index] = self._fail_count.get(proxy_index, 0) + 1
        if self._fail_count[proxy_index] >= _FAIL_THRESHOLD:
            self._cooldown_until[proxy_index] = datetime.utcnow() + timedelta(seconds=_COOLDOWN_SECONDS)
            logger.info("[ProxyPool] proxy #%d entering cooldown %ds",
                        proxy_index, _COOLDOWN_SECONDS)

    def status_snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "strategy": self.strategy,
            "proxies": [
                {
                    "name": p.get("name"),
                    "url": _mask_url(p.get("url", "")),
                    "weight": p.get("weight", 1),
                    "fail_count": self._fail_count.get(i, 0),
                    "cooldown_until": self._cooldown_until[i].isoformat() + "Z" if i in self._cooldown_until else None,
                }
                for i, p in enumerate(self._proxies)
            ],
        }


def _mask_url(url: str) -> str:
    """脱敏显示：隐藏 password 段"""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            creds, host = rest.rsplit("@", 1)
            if ":" in creds:
                user, _pw = creds.split(":", 1)
                return f"{scheme}://{user}:****@{host}"
    return url


# 进程级单例
_pool: Optional[ProxyPool] = None


def init_proxy_pool(config_dict: dict) -> ProxyPool:
    """从 config 初始化代理池（lifespan 时调用）"""
    global _pool
    _pool = ProxyPool(
        proxies=config_dict.get("proxies", []),
        strategy=config_dict.get("strategy", "round_robin"),
        enabled=bool(config_dict.get("enabled", False)),
    )
    return _pool


def get_proxy_pool() -> ProxyPool:
    global _pool
    if _pool is None:
        _pool = ProxyPool(enabled=False)
    return _pool
