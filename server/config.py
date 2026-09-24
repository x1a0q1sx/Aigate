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
    # 反代（nginx 等）后启用：日志/限速取 X-Forwarded-For 最左值。
    # 默认关闭——直连场景开 XFF 会被客户端伪造头。
    trust_proxy_headers: bool = False
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
    # P2: 日限（0 = 不限）。此前 rpd/tpd 只增不减也从不检查，等于永不生效。
    default_rpd: int = 0
    default_tpd: int = 0
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
    """刷新模型列表时的网络与并发参数。
    刷新会对上游 /v1/models 与定价接口各发一次请求，上游慢或不可达时
    timeout_seconds 决定单次最多等多久。"""
    timeout_seconds: int = 20               # 单次网络请求超时（list_models 与 pricing 各算一次）
    remove_missing_models: bool = True      # 刷新时自动删除上游已下架、本地仍存在的自动同步模型（保留手动添加的 is_manual=True）
    scheduled_enabled: bool = False         # 定时自动刷新全部服务商模型（默认关闭；每次落 model_refresh_logs，分析页可查详情）
    interval_minutes: int = 720             # 定时刷新间隔（分钟），仅 scheduled_enabled=true 时生效
    # ── 批量刷新并发（2026-09 新增：此前串行 57 个服务商，平均 17s/个 → 全量十几分钟）──
    # 生产实测（58 个服务商全量，单站超时 45s）：串行 ~16min → 6 并发 101s
    # → 12 并发 60s → 20 并发 52s（边际递减；再高主要压上游限流风险）
    concurrency: int = 12                   # 全量刷新时的并发服务商数（1=串行）
    provider_timeout_seconds: int = 45      # 单个服务商的整体硬超时：超过即判失败、不再等待
                                            # （单次请求超时是 timeout_seconds，这里是「整站上限」含定价）


class ArenaConfig(BaseModel):
    """LMSys Arena 排行榜（智力评分同步）配置。
    同步在启动时后台执行，不阻塞网关就绪。"""
    sync_on_startup: bool = True            # 是否在启动时拉取 Arena 排行榜同步智力分（false 则完全跳过，避免外网不可达时徒劳重试）
    timeout_seconds: int = 15               # 单次拉取超时（秒）
    sync_weekly: bool = True                # 每周一凌晨自动再同步一次（保持新模型评分跟进）


class BackupConfig(BaseModel):
    """数据库定时备份（C3）：每日自动备份 + 保留 N 份。
    SQLite 用在线备份 API（不锁库、不停服务）；PostgreSQL 调 pg_dump。"""
    enabled: bool = True
    dir: str = "./data/backups"             # 备份输出目录
    keep: int = 14                          # 最多保留份数（超出删最旧）
    hour: int = 3                           # 每日备份时刻（本地时区）
    minute: int = 30


class ResponseCacheConfig(BaseModel):
    """A4: 可选响应缓存。相同请求指纹（模型+全部参数）短 TTL 直接复用，
    省 token 与公益站额度。仅缓存非流式成功响应；默认关闭。"""
    enabled: bool = False
    ttl_seconds: int = 300                  # 缓存有效期
    max_items: int = 200                    # 最大缓存条数（LRU 淘汰）
    max_body_bytes: int = 262144            # 超过 256KB 的响应不缓存


class NotifyConfig(BaseModel):
    """D3: 事件通知。三类渠道可任选，事件可分别开关；同类事件节流防刷屏。"""
    enabled: bool = False
    webhook_url: str = ""                   # 通用 Webhook（POST JSON {text}）
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    dingtalk_webhook: str = ""              # 钉钉群机器人 Webhook
    notify_model_cooldown: bool = True      # 模型进入冷却
    notify_all_failed: bool = True          # 组合/auto 全部候选失败
    notify_budget_exceeded: bool = True     # 网关密钥预算超限
    notify_checkin: bool = True             # 每日签到失败（core/checkin.py）
    min_interval_seconds: int = 300         # 同类事件最小间隔（节流）


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

class DroolGuardConfig(BaseModel):
    """流口水自动冷却：同一模型连续 N 次返回"一字不差"的答案 → 判为复读，罚时冷却。
    只比对答案正文（忽略 chunk id 等信封差异）；过短回复不参与判定防误伤。"""
    enabled: bool = True
    threshold: int = 5             # 连续相同次数
    cooldown_minutes: int = 30     # 罚时时长（分钟）
    min_answer_chars: int = 40     # 归一化答案短于此不参与（"好的"之类正常短答复不算复读）

class RaceConfig(BaseModel):
    """组合/auto 候选竞速：当前候选 N 秒没返回内容 → 不等它，并行打下一候选，
    谁先出结果用谁；被超前的候选判失败并自动罚冷却。"""
    enabled: bool = True
    no_content_seconds: int = 15

class CheckinConfig(BaseModel):
    """每日签到：自动领取上游免费积分/额度（CodeBuddy / Qoder 等）。

    时间按**北京时间**（UTC+8）—— 上游活动按该时区刷新（Qoder 每日 10:00），
    默认 10:30 留 30 分钟余量。startup_catchup：服务重启错过时间点后自动补签当天。
    协议与实测见 core/checkin.py 模块 docstring。"""
    enabled: bool = True            # 自动签到总开关（默认开）
    hour: int = 10                  # 北京时间（0-23）
    minute: int = 30                # 0-59
    startup_catchup: bool = True    # 启动时若当天未签则补签

class OpenCodeBridgeConfig(BaseModel):
    """OpenCode 免费层的官方 CLI 桥接（sidecar）。

    上游把免费层入口锁在「官方 CLI 进程建立的会话」里，纯 HTTP 转发一律 403，
    故网关经服务器上常驻的 `opencode serve` 转发（见 core/opencode_sidecar.py）。
    环境变量仍可覆盖（AIGATE_OPENCODE_*），本配置段优先。
    """
    enabled: bool = True
    timeout_seconds: int = 180          # 单次请求最长等待（CLI 生成完成为止）
    poll_interval_ms: int = 600         # 轮询 sidecar 消息列表的间隔
    stall_grace_seconds: int = 20       # 本轮多久没有新进展即判停滞（interrupt 收尾，不再空等）
    agent: str = "aigate"               # CLI 侧 agent 名（决定人格与工具权限）
    base_url: str = "http://127.0.0.1:4096"   # sidecar 地址（仅本机可达）
    port: int = 4096                    # 守护任务拉起 CLI 时监听的端口
    bin_path: str = ""                  # CLI 可执行文件路径；留空 = 自动探测
    auto_start: bool = True             # sidecar 掉线时自动拉起
    manage_agent_config: bool = True    # 启动时写入/修正 CLI 的 agent 定义
    # 无人值守护栏：客户端 system prompt（如编码 agent 模板）会诱导模型调用工具，
    # 而 CLI 默认 permission=ask 会无限期等待人工批准 → 消息永不 completed → 网关超时。
    # 开启后自动拒绝挂起的权限请求，CLI 立刻收到工具错误并继续把答案写完。
    auto_reject_tools: bool = True

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
    backup: BackupConfig = Field(default_factory=BackupConfig)
    response_cache: ResponseCacheConfig = Field(default_factory=ResponseCacheConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    token_saver_extra: TokenSaverExtraConfig = Field(default_factory=TokenSaverExtraConfig)
    headroom: HeadroomConfig = Field(default_factory=HeadroomConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    drool_guard: DroolGuardConfig = Field(default_factory=DroolGuardConfig)
    race: RaceConfig = Field(default_factory=RaceConfig)
    checkin: CheckinConfig = Field(default_factory=CheckinConfig)
    opencode_bridge: OpenCodeBridgeConfig = Field(default_factory=OpenCodeBridgeConfig)
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
        config_path = os.environ.get("AIGATE_CONFIG_PATH", "config.yaml")
        _config = load_config(config_path)
        _config = ensure_encryption_key(_config, config_path)
        database_path = os.environ.get("AIGATE_DATABASE_PATH")
        if database_path:
            _config.database.path = database_path
    return _config
def save_config():
    """持久化当前配置到 config.yaml"""
    import yaml
    path = Path(os.environ.get("AIGATE_CONFIG_PATH", "config.yaml"))
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(get_config().model_dump(), f, default_flow_style=False, allow_unicode=True)
