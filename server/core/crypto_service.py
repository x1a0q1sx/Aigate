"""
加密服务
AES-256-GCM (via cryptography Fernet)
"""
from typing import Optional
from cryptography.fernet import Fernet
from server.config import get_config
config = get_config()
class CryptoService:
    def __init__(self):
        key = config.security.encryption_key
        if not key:
            raise ValueError("加密密钥未配置，请检查 config.yaml")
        self._fernet = Fernet(key.encode('utf-8'))
    def encrypt(self, plaintext: str) -> str:
        """加密明文"""
        return self._fernet.encrypt(plaintext.encode('utf-8')).decode('utf-8')
    def decrypt(self, ciphertext: str) -> str:
        """解密密文"""
        return self._fernet.decrypt(ciphertext.encode('utf-8')).decode('utf-8')
    @staticmethod
    def mask_key(key: str) -> str:
        """密钥脱敏展示，保留前3后4"""
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:3]}****{key[-4:]}"
# 全局单例
_crypto: Optional[CryptoService] = None
def get_crypto_service() -> CryptoService:
    global _crypto
    if _crypto is None:
        _crypto = CryptoService()
    return _crypto