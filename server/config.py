"""
配置管理模块
读取 YAML 配置 + 环境变量
"""
import os
from pathlib import Path
from typing import List, Optional
import yaml
from pydantic import BaseModel, Field
from cryptography.fernet import Fernet
class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])
class DatabaseConfig(BaseModel):
    path: str = "./data/aigate.db"
class SecurityConfig(BaseModel):
    encryption_key: str = ""
    aigate_api_key: str = ""
class HealthCheckConfig(BaseModel):
    interval_minutes: int = 5
    ping_timeout_seconds: int = 10
    max_history_per_model: int = 100
    healthy_latency_threshold_ms: float = 2000.0
class AutoRouterConfig(BaseModel):
    max_fallbacks: int = 5
    cooling_period_seconds: int = 30
    session_sticky_minutes: int = 30
    free_model_priority: bool = True
class RateLimitConfig(BaseModel):
    default_rpm: int = 60
    default_tpm: int = 100000
class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: Optional[str] = None
class LogArchiveConfig(BaseModel):
    """请求日志每日归档策略"""
    enabled: bool = True
    archive_dir: str = "./data/archives"  # 归档文件目录
class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    auto_router: AutoRouterConfig = Field(default_factory=AutoRouterConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    log_archive: LogArchiveConfig = Field(default_factory=LogArchiveConfig)
def load_config(config_path: str = "config.yaml") -> Config:
    """加载配置文件，如果不存在则创建默认"""
    path = Path(config_path)
    if not path.exists():
        default_config = Config()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(default_config.model_dump(), f, default_flow_style=False, allow_unicode=True)
        return default_config
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Config(**data) if data else Config()
def ensure_encryption_key(config: Config, config_path: str) -> Config:
    """确保加密密钥存在，如果不存在则生成"""
    changed = False
    if not config.security.encryption_key:
        key = Fernet.generate_key().decode('utf-8')
        config.security.encryption_key = key
        changed = True
    if not config.security.aigate_api_key:
        import secrets
        config.security.aigate_api_key = "ak-" + secrets.token_urlsafe(32)
        changed = True
    if changed:
        # 保存回配置文件
        path = Path(config_path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(config.model_dump(), f, default_flow_style=False, allow_unicode=True)
        print(f"\n⚠️  首次启动：已生成新的安全配置，请备份 {config_path} 到安全位置！")
        print(f"   加密密钥: {config.security.encryption_key}")
        print(f"   AIGate 访问密钥: {config.security.aigate_api_key}\n")
    return config
# 全局配置实例
_config: Optional[Config] = None
def get_config() -> Config:
    """获取全局配置"""
    global _config
    if _config is None:
        _config = load_config()
        _config = ensure_encryption_key(_config, "config.yaml")
    return _config
def save_config():
    """持久化当前配置到 config.yaml"""
    import yaml
    path = Path("config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(get_config().model_dump(), f, default_flow_style=False, allow_unicode=True)