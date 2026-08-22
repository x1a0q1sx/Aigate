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
    # Interactive streamed auto routing must yield a useful response promptly.
    stream_first_chunk_timeout_seconds: int = 20
    stream_first_response_budget_seconds: int = 75
class RateLimitConfig(BaseModel):
    default_rpm: int = 60
    default_tpm: int = 100000
class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: Optional[str] = None
    verbose_diag: bool = False   # 请求诊断日志是否输出全部阶段（默认 False=精简，仅保留关键里程碑）
class LogArchiveConfig(BaseModel):
    """请求日志每日归档策略"""
    enabled: bool = True
    archive_dir: str = "./data/archives"  # 归档文件目录
class TokenSaverConfig(BaseModel):
    """RTK Token Saver — 注入式 Prompt 压缩器配置"""
    enabled: bool = True              # 总开关，默认开启
    min_chars: int = 80               # 小于该长度的 system/user 不动
    log_savings: bool = False         # 是否在请求日志里记录节省字符数（调试用）
class ComboConfig(BaseModel):
    """Combos 组合路由配置"""
    enabled: bool = True
    default_strategy: str = "fallback"   # fallback / round_robin / fusion
    max_fallbacks: int = 5               # 单次 combo 调用最多重试几个模型


class ProxyPoolConfig(BaseModel):
    """HTTP 代理池配置"""
    enabled: bool = False
    strategy: str = "round_robin"           # round_robin / weighted / random
    proxies: List[dict] = Field(default_factory=list)


class ModelRefreshConfig(BaseModel):
    """刷新模型列表时的网络超时（秒）。
    刷新会顺序请求上游 /v1/models 与定价接口，上游慢或不可达时这里决定最多等多久。"""
    timeout_seconds: int = 20               # 单次网络请求超时（list_models 与 pricing 各算一次）
    remove_missing_models: bool = True      # 刷新时自动删除上游已下架、本地仍存在的自动同步模型（保留手动添加的 is_manual=True）


class ArenaConfig(BaseModel):
    """LMSys Arena 排行榜（智力评分同步）配置。
    同步在启动时后台执行，不阻塞网关就绪。"""
    sync_on_startup: bool = True            # 是否在启动时拉取 Arena 排行榜同步智力分（false 则完全跳过，避免外网不可达时徒劳重试）
    timeout_seconds: int = 15               # 单次拉取超时（秒）


# 注：配额追踪已合并到分析页，原 QuotaConfig 阈值配置已删除


class TokenSaverExtraConfig(BaseModel):
    """高级 token saver：Caveman / Ponytail（默认关闭，保守启用）"""
    caveman_enabled: bool = False
    ponytail_enabled: bool = False


class HeadroomConfig(BaseModel):
    """Headroom：保留部分 provider 额度（不入自动 routing 候选池）"""
    enabled: bool = False
    entries: List[dict] = Field(default_factory=list)   # [{provider_id, daily_token_limit, label}]

class AuthConfig(BaseModel):
    """管理面板登录认证"""
    enabled: bool = True
    username: str = "admin"
    password_hash: str = ""          # bcrypt hash，首次启动自动生成默认密码
    session_timeout_hours: int = 24  # session 有效时长

class OpenAICompatConfig(BaseModel):
    """openai_compat 适配器行为调优"""
    # 上游（如 grok-4.5）常返回 reasoning_content（思考流）。
    # 部分客户端不支持该字段，会把思考流当成可见文本逐片渲染成“子弹列表/乱码”。
    #   passthrough: 保留思考流并合并成较大块（默认；支持 reasoning 的客户端可显示思考过程）
    #   drop:        彻底丢弃思考流，只返回 content（适合不支持 reasoning 的客户端）
    reasoning: str = "passthrough"
    # content 合并阈值（字符数），避免上游逐字符吐字造成的碎片/串行空格
    content_chunk_size: int = 24

class AdaptersConfig(BaseModel):
    openai_compat: OpenAICompatConfig = Field(default_factory=OpenAICompatConfig)
class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    auto_router: AutoRouterConfig = Field(default_factory=AutoRouterConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    log_archive: LogArchiveConfig = Field(default_factory=LogArchiveConfig)
    token_saver: TokenSaverConfig = Field(default_factory=TokenSaverConfig)
    combos: ComboConfig = Field(default_factory=ComboConfig)
    proxy_pool: ProxyPoolConfig = Field(default_factory=ProxyPoolConfig)
    model_refresh: ModelRefreshConfig = Field(default_factory=ModelRefreshConfig)
    arena: ArenaConfig = Field(default_factory=ArenaConfig)
    token_saver_extra: TokenSaverExtraConfig = Field(default_factory=TokenSaverExtraConfig)
    headroom: HeadroomConfig = Field(default_factory=HeadroomConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
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
    # 首次启动自动生成管理面板默认密码。
    # 可用环境变量 AIGATE_DEFAULT_PASSWORD 指定初始密码；未设置时才回退到内置默认值。
    if config.auth.enabled and not config.auth.password_hash:
        import bcrypt
        default_password = os.environ.get("AIGATE_DEFAULT_PASSWORD", "aigate123")
        config.auth.password_hash = bcrypt.hashpw(
            default_password.encode(), bcrypt.gensalt()
        ).decode()
        changed = True
        print(f"\n⚠️  管理面板默认登录: 用户名={config.auth.username} 密码={default_password}")
        print(f"   请登录后立即在配置文件中修改密码！\n")
    if changed:
        # 保存回配置文件
        path = Path(config_path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(config.model_dump(), f, default_flow_style=False, allow_unicode=True)
        print(f"\n⚠️  首次启动：已生成新的安全配置，请备份 {config_path} 到安全位置！")
        print(f"   加密密钥与 AIGate 访问密钥已写入 {config_path}，请勿外泄。\n")
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
