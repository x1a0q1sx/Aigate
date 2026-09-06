"""
数据库初始化模块
"""
import os
import warnings
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import text
from sqlalchemy.dialects.sqlite.aiosqlite import AsyncAdapt_aiosqlite_connection

# ── 修补 aiosqlite 的 terminate_force_close() 未实现问题 ──
# 当客户端断开导致 CancelledError 时，SQLAlchemy 尝试 force-close 连接，
# 但 aiosqlite 没实现该方法。此处 monkey-patch 使其静默忽略。
_original_terminate = AsyncAdapt_aiosqlite_connection.terminate

def _safe_terminate(self):
    try:
        _original_terminate(self)
    except NotImplementedError:
        pass  # aiosqlite 不支持 force close，连接由 GC 回收

AsyncAdapt_aiosqlite_connection.terminate = _safe_terminate

# 抑制 GC 回收连接时的 SAWarning（SQLite 连接泄漏无害，GC 会自动清理）
warnings.filterwarnings("ignore", message=".*non-checked-in connection.*")
from .models.base import Base
# 导入所有 ORM 模型（确保 create_all 能建全）
from .models.provider import Provider
from .models.api_key import ApiKey
from .models.model import Model
from .models.model_api_key import ModelApiKey  # v3.5 模型级密钥归属关联表（create_all 自动建表）
from .models.health_check import HealthCheck
from .models.rate_limit import RateLimitState
from .models.request_log import RequestLog, LogMsgBlob, AnalyticsCumulative  # v3.6 消息级去重 blob 仓库
from .models.intelligence import IntelligenceStatic
from .models.routing_config import RoutingWeights, RoutingPin, AdminAuditLog
from .models.combo import Combo
from .models.oauth_token import OAuthToken
from .models.route_decision import RouteDecision
from .config import get_config
config = get_config()
# P2-11: DATABASE_URL 环境变量优先（postgresql+asyncpg://...），未设置时用 SQLite 单机默认
import os as _os
DATABASE_URL = _os.environ.get("AIGATE_DATABASE_URL") or f"sqlite+aiosqlite:///{config.database.path}"
IS_SQLITE = DATABASE_URL.startswith("sqlite")
db_path = Path(config.database.path)
db_path.parent.mkdir(parents=True, exist_ok=True)

_engine_kwargs = dict(echo=False, pool_pre_ping=True)
if IS_SQLITE:
    _engine_kwargs["connect_args"] = {
        "check_same_thread": False,
        "timeout": 15,  # 等待锁的超时（秒），默认 5 秒对并发场景偏短
    }
_engine_kwargs["pool_size"] = 20      # 连接池大小，适应并发刷新+多请求并发
_engine_kwargs["max_overflow"] = 10   # 超出 pool_size 时可额外创建的数量
engine = create_async_engine(DATABASE_URL, **_engine_kwargs)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
# v0.2 智力静态种子（业界普遍认知）
INTEL_SEEDS = [
    ("claude-opus-4-*", 96, "S", "顶级推理与写作"),
    ("gpt-5*", 95, "S", "综合最强"),
    ("o3-pro", 95, "S", "深度推理"),
    ("claude-sonnet-4-*", 88, "A", "强推理"),
    ("gpt-4.1*", 87, "A", "综合强"),
    ("gemini-2.5-pro", 87, "A", "长上下文强"),
    ("deepseek-r1*", 85, "A", "开源推理 SOTA"),
    ("gpt-4o", 78, "B", "中等"),
    ("claude-haiku-4*", 78, "B", "快速中等"),
    ("gemini-2.5-flash", 76, "B", "快速"),
    ("qwen3-235b*", 75, "B", "开源中强"),
    ("llama-3.3-70b*", 75, "B", "开源中强"),
    ("gpt-4o-mini", 70, "C", "轻量"),
    ("gemma-3-27b*", 70, "C", "轻量"),
    ("qwen3-32b*", 65, "C", "轻量"),
    ("llama-3.1-8b*", 60, "C", "轻量"),
]
async def init_db():
    """初始化数据库表 + 增量迁移（每条独立事务）+ WAL 模式（SQLite）+ 种子。

    P2-11: PostgreSQL 事务 abort 语义下，迁移循环里一条失败会连累全部后续
    语句，因此每条迁移用独立事务执行、失败跳过该条；SQLite 行为一致。
    """
    async with engine.begin() as conn:
        if IS_SQLITE:
            # SQLite 专用：WAL 允许并发读写避免 SQLITE_BUSY；外键需显式开启
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)

    # 增量迁移（幂等：已存在/已迁移的语句失败即跳过）
    _migrations = [
        "ALTER TABLE providers ADD COLUMN credential_type VARCHAR(20) DEFAULT 'api_key'",
        "ALTER TABLE providers ADD COLUMN oauth_code VARCHAR(50) DEFAULT NULL",
        "ALTER TABLE providers ADD COLUMN enabled BOOLEAN DEFAULT 1",
        "ALTER TABLE providers ADD COLUMN proxy_url VARCHAR(500) DEFAULT NULL",
        "ALTER TABLE providers ADD COLUMN proxy_enabled BOOLEAN DEFAULT 0",
        "ALTER TABLE models ADD COLUMN priority_boost INTEGER DEFAULT 0",
        "ALTER TABLE models ADD COLUMN auto_excluded BOOLEAN DEFAULT 0",
        "ALTER TABLE models ADD COLUMN manual_cooldown_until TIMESTAMP",
        "ALTER TABLE models ADD COLUMN auto_cooldown_until TIMESTAMP DEFAULT NULL",
        "ALTER TABLE models ADD COLUMN auto_fail_count INTEGER DEFAULT 0",
        "ALTER TABLE models ADD COLUMN success_rate REAL",
        "ALTER TABLE models ADD COLUMN avg_latency_ms REAL",
        "ALTER TABLE models ADD COLUMN avg_ttft_ms REAL",
        "ALTER TABLE models ADD COLUMN avg_tps REAL",
        "ALTER TABLE models ADD COLUMN pricing_source VARCHAR(500) DEFAULT ''",
        "ALTER TABLE models ADD COLUMN pricing_updated_at TIMESTAMP",
        "ALTER TABLE models ADD COLUMN request_overrides TEXT DEFAULT NULL",
        "ALTER TABLE models ADD COLUMN is_manual BOOLEAN DEFAULT 0",
        "ALTER TABLE request_logs ADD COLUMN routed_provider_id INTEGER",
        "ALTER TABLE request_logs ADD COLUMN estimated_cost_usd REAL DEFAULT 0.0",
        "ALTER TABLE request_logs ADD COLUMN used_proxy BOOLEAN DEFAULT 0",
        "ALTER TABLE request_logs ADD COLUMN proxy_url VARCHAR(255) DEFAULT NULL",
        "ALTER TABLE request_logs ADD COLUMN is_health_check BOOLEAN DEFAULT 0",
        "ALTER TABLE intelligence_static ADD COLUMN source VARCHAR(20) DEFAULT 'arena'",
        "DROP TABLE IF EXISTS quota_usage",
        "DELETE FROM rate_limits WHERE id NOT IN (SELECT MIN(id) FROM rate_limits GROUP BY model_id, key_id)",
        "DELETE FROM model_api_keys WHERE model_id NOT IN (SELECT id FROM models)",
        "DELETE FROM model_api_keys WHERE api_key_id NOT IN (SELECT id FROM api_keys)",
        # 孤儿清理（PG 迁移实测发现：SQLite 不强制外键，rate_limits/health_checks
        # 残留引用已删除 model/key 的行；幂等，每次启动兜底扫一遍）
        "DELETE FROM rate_limits WHERE model_id IS NOT NULL AND model_id NOT IN (SELECT id FROM models)",
        "DELETE FROM rate_limits WHERE key_id IS NOT NULL AND key_id NOT IN (SELECT id FROM api_keys)",
        "DELETE FROM health_checks WHERE model_id IS NOT NULL AND model_id NOT IN (SELECT id FROM models)",
        "ALTER TABLE models ADD COLUMN cache_read_input_price REAL DEFAULT 0.0",
        "ALTER TABLE models ADD COLUMN cache_write_input_price REAL DEFAULT 0.0",
        "ALTER TABLE models ADD COLUMN supports_reasoning_effort BOOLEAN DEFAULT NULL",
        "ALTER TABLE request_logs ADD COLUMN cache_read_tokens INTEGER",
        "ALTER TABLE request_logs ADD COLUMN cache_write_tokens INTEGER",
        "ALTER TABLE request_logs ADD COLUMN ttft_ms INTEGER DEFAULT NULL",
        "ALTER TABLE request_logs ADD COLUMN archived_at DATETIME DEFAULT NULL",
        "ALTER TABLE request_logs ADD COLUMN est_prompt_tokens INTEGER DEFAULT NULL",
        "ALTER TABLE models ADD COLUMN observed_context_limit INTEGER DEFAULT NULL",
        "ALTER TABLE models ADD COLUMN context_source VARCHAR(50) DEFAULT ''",
        "ALTER TABLE models ADD COLUMN capability_source VARCHAR(50) DEFAULT ''",
        "ALTER TABLE analytics_cumulative ADD COLUMN sum_ttft_ms INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE analytics_cumulative ADD COLUMN ttft_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE request_logs ADD COLUMN request_env_hash VARCHAR(64) DEFAULT NULL",
        "ALTER TABLE request_logs ADD COLUMN request_msg_hashes TEXT DEFAULT NULL",
        "ALTER TABLE request_logs ADD COLUMN response_body_hash VARCHAR(64) DEFAULT NULL",
    ]
    for sql in _migrations:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass

    # combos / oauth_tokens 建表（SQLite 专用：AUTOINCREMENT 语法 PG 不兼容，
    # PG 的表由上方 create_all 按 ORM 模型创建）
    if IS_SQLITE:
        for sql in [
            """CREATE TABLE IF NOT EXISTS combos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                strategy VARCHAR(20) DEFAULT 'fallback',
                model_ids JSON NOT NULL,
                priority INTEGER DEFAULT 0,
                enabled BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_combos_name ON combos(name)",
        ]:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(sql))
            except Exception:
                pass

    # 回填（幂等）
    for sql in [
        "UPDATE request_logs SET is_health_check=1 WHERE conversation_id LIKE 'hc-%' AND is_health_check=0",
        "UPDATE providers SET proxy_enabled = 1 "
        "WHERE proxy_enabled = 0 AND proxy_url IS NOT NULL AND TRIM(proxy_url) != ''",
    ]:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass

    # 索引（CREATE INDEX IF NOT EXISTS 双方言兼容）
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_request_logs_model_time ON request_logs(routed_model, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_status_time ON request_logs(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_conv_time ON request_logs(conversation_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_hc_time ON request_logs(is_health_check, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_time ON admin_audit_log(created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limits_model_key ON rate_limits(model_id, key_id)",
        "CREATE INDEX IF NOT EXISTS idx_log_msg_blobs_hash ON log_msg_blobs(hash)",
        "CREATE INDEX IF NOT EXISTS idx_oauth_provider_owner ON oauth_tokens(provider_code, owner)",
    ]:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass

    # 种子（查后插，幂等，方言无关）
    async with engine.begin() as conn:
        if not (await conn.execute(text("SELECT 1 FROM routing_weights WHERE id=1"))).first():
            await conn.execute(text(
                "INSERT INTO routing_weights (id, w_speed, w_intel, w_stab, updated_at) "
                "VALUES (1, 0.30, 0.50, 0.20, CURRENT_TIMESTAMP)"
            ))
        if not (await conn.execute(text("SELECT 1 FROM routing_pin WHERE id=1"))).first():
            await conn.execute(text("INSERT INTO routing_pin (id, updated_at) VALUES (1, CURRENT_TIMESTAMP)"))
        if not (await conn.execute(text("SELECT 1 FROM analytics_cumulative WHERE id=1"))).first():
            await conn.execute(text(
                "INSERT INTO analytics_cumulative "
                "(id, total_requests, success_count, auto_requests, total_input_tokens, "
                "total_output_tokens, sum_latency_ms, latency_count, sum_ttft_ms, ttft_count, updated_at) "
                "VALUES (1, 0, 0, 0, 0, 0, 0, 0, 0, 0, CURRENT_TIMESTAMP)"
            ))
        for pattern, score, tier, notes in INTEL_SEEDS:
            if (await conn.execute(text(
                "SELECT 1 FROM intelligence_static WHERE pattern = :p"
            ), {"p": pattern})).first():
                continue
            await conn.execute(text(
                "INSERT INTO intelligence_static (pattern, score, tier, notes, source, updated_at) "
                "VALUES (:p, :s, :t, :n, 'manual', CURRENT_TIMESTAMP)"
            ), {"p": pattern, "s": score, "t": tier, "n": notes})


create_tables = init_db
