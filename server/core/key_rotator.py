"""
KeyRotator — 多密钥轮换 + 单 key 熔断
策略：
  1) 维护 provider_id → active key 列表（懒加载）
  2) 轮换游标在内存中保存（进程重启从 0 开始）
  3) 401/429/网络异常 → 该 key 标记 fail_count++
  4) fail_count >= threshold (默认 3) → 暂时熔断（放冷却池 60s 可恢复，避免永久摘除）
  5) pick 时跳过熔断 + 冷却期未到的 key

设计上不修改原有 key_manager.decrypt_key，避免破坏现有逻辑。
新增 pick_active_key() 方法返回 (key_id, plaintext) 把轮换决策一站式收口。
"""
from __future__ import annotations
import time
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from server.models.api_key import ApiKey
from server.models.provider import Provider
from .crypto_service import get_crypto_service, CryptoService

logger = logging.getLogger(__name__)

# 单 key 失败 3 次进入熔断
_FAIL_THRESHOLD = 3
# 熔断后冷却 60 秒，过期自动恢复
_COOLDOWN_SECONDS = 60
# 401 / 403 直接永久禁用（除非人工重新激活），不进入有限熔断循环
_HARD_FAILURE_CODES = {401, 403}


class KeyRotator:
    """多密钥轮换 + 熔断（per-provider instance）"""

    def __init__(self, crypto: CryptoService = None):
        self._crypto = crypto or get_crypto_service()
        # 内存状态
        self._cursor: Dict[int, int] = {}                 # provider_id → next idx
        self._fail_count: Dict[int, int] = {}              # api_key_id → 连续失败次数
        self._cooldown_until: Dict[int, datetime] = {}    # api_key_id → 恢复时间
        self._hard_disabled: set = set()                    # api_key_id → 401/403 永久禁用（进程内）

    def _is_available(self, key_id: int) -> bool:
        if key_id in self._hard_disabled:
            return False
        cd = self._cooldown_until.get(key_id)
        if cd and datetime.utcnow() < cd:
            return False
        # 冷却过期 → 自动恢复
        if cd and datetime.utcnow() >= cd:
            self._cooldown_until.pop(key_id, None)
            self._fail_count.pop(key_id, None)
        return True

    async def list_active_keys(self, db: AsyncSession, provider_id: int) -> List[ApiKey]:
        """DB 真实查询：provider 下所有 is_active 的密钥（刷新缓存不持有 ORM）"""
        result = await db.execute(
            select(ApiKey).where(
                ApiKey.provider_id == provider_id,
                ApiKey.is_active == True
            ).order_by(ApiKey.id)
        )
        return list(result.scalars().all())

    async def pick_active_key(self, db: AsyncSession, provider_id: int) -> Optional[Tuple[int, str]]:
        """
        从该 provider 的密钥池中挑选可用的一把。
        返回 (api_key_id, plaintext_key)，无可用时返回 None。
        """
        keys = await self.list_active_keys(db, provider_id)
        if not keys:
            return None
        # 过滤：跳过 hard_disabled 和冷却中的
        avail = [k for k in keys if self._is_available(k.id)]
        if not avail:
            return None
        # round-robin
        cursor = self._cursor.get(provider_id, 0) % len(avail)
        chosen = avail[cursor]
        self._cursor[provider_id] = (cursor + 1) % len(avail)
        plaintext = self._crypto.decrypt(chosen.key_encrypted)
        return chosen.id, plaintext

    def mark_success(self, api_key_id: int):
        """单次请求成功 → 清空 fail_count"""
        if api_key_id:
            self._fail_count.pop(api_key_id, None)
            self._cooldown_until.pop(api_key_id, None)

    def mark_failure(self, api_key_id: int, status_code: Optional[int] = None):
        """单次请求失败 → 计数加 1，过阈值则进冷却；401/403 永久禁用"""
        if not api_key_id:
            return
        if status_code in _HARD_FAILURE_CODES:
            self._hard_disabled.add(api_key_id)
            logger.warning("key %s hard-disabled (status %s)", api_key_id, status_code)
            return
        self._fail_count[api_key_id] = self._fail_count.get(api_key_id, 0) + 1
        if self._fail_count[api_key_id] >= _FAIL_THRESHOLD:
            self._cooldown_until[api_key_id] = datetime.utcnow() + timedelta(seconds=_COOLDOWN_SECONDS)
            logger.info("key %s entering cooldown %ds after %d fails",
                        api_key_id, _COOLDOWN_SECONDS, self._fail_count[api_key_id])

    def status_snapshot(self) -> dict:
        """供 admin API 读取内部运行时状态（供前端配额面板展示）"""
        return {
            "fail_count": dict(self._fail_count),
            "cooldown_until": {k: v.isoformat() + "Z" for k, v in self._cooldown_until.items()},
            "hard_disabled": list(self._hard_disabled),
            "cursor": dict(self._cursor),
        }


# 进程级单例
_rotator: Optional[KeyRotator] = None


def get_key_rotator() -> KeyRotator:
    global _rotator
    if _rotator is None:
        _rotator = KeyRotator()
    return _rotator
