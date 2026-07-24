"""
密钥管理器
加密存储 + 解密 + 脱敏
"""
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from server.models.api_key import ApiKey
from server.models.provider import Provider
from .crypto_service import get_crypto_service, CryptoService
class KeyManager:
    def __init__(self, crypto: CryptoService = None):
        self._crypto = crypto or get_crypto_service()
    async def list_keys(
        self,
        session: AsyncSession,
        provider_id: Optional[int] = None
    ) -> List[ApiKey]:
        """列出所有密钥（不包含密文）"""
        query = select(ApiKey).where(ApiKey.is_active == True)
        if provider_id is not None:
            query = query.where(ApiKey.provider_id == provider_id)
        result = await session.execute(query)
        return list(result.scalars().all())
    async def add_key(
        self,
        session: AsyncSession,
        provider_id: int,
        plaintext_key: str,
        label: str = ""
    ) -> ApiKey:
        """添加新密钥，自动加密存储"""
        # 提取前缀用于展示
        prefix = plaintext_key[:3] if len(plaintext_key) >= 3 else plaintext_key
        encrypted = self._crypto.encrypt(plaintext_key)
        key = ApiKey(
            provider_id=provider_id,
            key_encrypted=encrypted,
            key_prefix=prefix,
            label=label,
            is_active=True
        )
        session.add(key)
        await session.commit()
        await session.refresh(key)
        return key
    async def decrypt_key(self, session: AsyncSession, key_id: int) -> Optional[str]:
        """解密密钥"""
        result = await session.execute(select(ApiKey).where(ApiKey.id == key_id))
        key = result.scalar_one_or_none()
        if not key or not key.is_active:
            return None
        return self._crypto.decrypt(key.key_encrypted)
    async def delete_key(self, session: AsyncSession, key_id: int) -> bool:
        """删除密钥"""
        result = await session.execute(select(ApiKey).where(ApiKey.id == key_id))
        key = result.scalar_one_or_none()
        if not key:
            return False
        await session.delete(key)
        await session.commit()
        return True
    def mask_key(self, key: str) -> str:
        """脱敏展示"""
        return self._crypto.mask_key(key)