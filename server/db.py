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
db_path = Path(config.database.path)
db_path.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite+aiosqlite:///{config.database.path}"
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={
        "check_same_thread": False,
        "timeout": 15,  # 等待锁的超时（秒），默认 5 秒对并发场景偏短
    },
    pool_size=20,      # 连接池大小，适应并发刷新+多请求并发
    max_overflow=10,    # 超出 pool_size 时可额外创建的数量（并发刷新不饿死其它请求）
    pool_pre_ping=True, # 连接复用前检测有效性
)
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
    """初始化数据库表 + 增量迁移 + WAL 模式 + v0.2 种子"""
    async with engine.begin() as conn:
        # 启用 WAL 模式：允许并发读写，避免 SQLITE_BUSY
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        # 启用外键约束：SQLite 默认不执行 ON DELETE CASCADE，必须显式开启
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)
        # v3.0 增量迁移
        for sql in [
            "ALTER TABLE providers ADD COLUMN credential_type VARCHAR(20) DEFAULT 'api_key'",
            # v3.1: oauth_code — 当 credential_type=oauth 时明确指向 oauth_registry code
            "ALTER TABLE providers ADD COLUMN oauth_code VARCHAR(50) DEFAULT NULL",
            # v4.0: 服务商启用/禁用开关（默认启用）
            "ALTER TABLE providers ADD COLUMN enabled BOOLEAN DEFAULT 1",
            "ALTER TABLE providers ADD COLUMN proxy_url VARCHAR(500) DEFAULT NULL",
            "ALTER TABLE providers ADD COLUMN proxy_enabled BOOLEAN DEFAULT 0",
        ]:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass
        # Providers configured during the short-lived per-provider URL experiment become switches.
        await conn.execute(text(
            "UPDATE providers SET proxy_enabled = 1 "
            "WHERE proxy_enabled = 0 AND proxy_url IS NOT NULL AND TRIM(proxy_url) != ''"
        ))
        # 新增 combos 表
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
            """CREATE TABLE IF NOT EXISTS oauth_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_code VARCHAR(50) NOT NULL,
                owner VARCHAR(100) NOT NULL DEFAULT '__default',
                access_token_enc TEXT NOT NULL,
                refresh_token_enc TEXT,
                token_type VARCHAR(20) DEFAULT 'Bearer',
                scope VARCHAR(500) DEFAULT '',
                expires_at TIMESTAMP,
                refresh_expires_at TIMESTAMP,
                is_active BOOLEAN DEFAULT 1,
                last_refreshed_at TIMESTAMP,
                last_error VARCHAR(500) DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_oauth_provider_owner ON oauth_tokens(provider_code, owner)",
        ]:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass
        # v1/v2 增量迁移
        for sql in [
            "ALTER TABLE models ADD COLUMN priority_boost INTEGER DEFAULT 0",
            "ALTER TABLE models ADD COLUMN auto_excluded BOOLEAN DEFAULT 0",
            "ALTER TABLE models ADD COLUMN manual_cooldown_until TIMESTAMP",
            # v9: 自动失败冷却持久化，重启后继续保留冷却惩罚
            "ALTER TABLE models ADD COLUMN auto_cooldown_until TIMESTAMP DEFAULT NULL",
            "ALTER TABLE models ADD COLUMN auto_fail_count INTEGER DEFAULT 0",
            "ALTER TABLE models ADD COLUMN success_rate REAL",
            "ALTER TABLE models ADD COLUMN avg_latency_ms REAL",
            "ALTER TABLE models ADD COLUMN avg_ttft_ms REAL",
            "ALTER TABLE models ADD COLUMN avg_tps REAL",
            "ALTER TABLE models ADD COLUMN pricing_source VARCHAR(500) DEFAULT ''",
            "ALTER TABLE models ADD COLUMN pricing_updated_at TIMESTAMP",
            # v3.4: per-model request overrides (CCSwitch ??)
            "ALTER TABLE models ADD COLUMN request_overrides TEXT DEFAULT NULL",
            "ALTER TABLE models ADD COLUMN is_manual BOOLEAN DEFAULT 0",
            # 方案A：配额追踪并入分析，request_logs 作为唯一用量数据源
            "ALTER TABLE request_logs ADD COLUMN routed_provider_id INTEGER",
            "ALTER TABLE request_logs ADD COLUMN estimated_cost_usd REAL DEFAULT 0.0",
            # v7: 记录请求是否走代理及实际代理 URL（详情页可见）
            "ALTER TABLE request_logs ADD COLUMN used_proxy BOOLEAN DEFAULT 0",
            "ALTER TABLE request_logs ADD COLUMN proxy_url VARCHAR(255) DEFAULT NULL",
            # v7.1: 健康检查探测标记列，让列表/聚合查询用等值过滤+索引替代 NOT LIKE
            "ALTER TABLE request_logs ADD COLUMN is_health_check BOOLEAN DEFAULT 0",
            # v8: intelligence_static 增加 source 列（manual=手工校准 / arena=Arena 同步）
            # 非破坏性同步：arena 同步只更新 source='arena' 的行，绝不覆盖手工校准
            "ALTER TABLE intelligence_static ADD COLUMN source VARCHAR(20) DEFAULT 'arena'",
            # 回填（幂等：仅把尚未标记的 hc-% 行置 1；列不存在时 except 跳过）
            "UPDATE request_logs SET is_health_check=1 WHERE conversation_id LIKE 'hc-%' AND is_health_check=0",
            # 删除旧的平行账本（数据已并入 request_logs）
            "DROP TABLE IF EXISTS quota_usage",
            # v8.1: 清理 rate_limits 历史重复行（并发创建导致 (model_id,key_id) 重复，会引发 MultipleResultsFound）
            "DELETE FROM rate_limits WHERE id NOT IN (SELECT MIN(id) FROM rate_limits GROUP BY model_id, key_id)",
            # v13.1: 清理 model_api_keys 孤儿关联（SQLite 外键默认关闭，删模型/密钥时未级联）
            "DELETE FROM model_api_keys WHERE model_id NOT IN (SELECT id FROM models)",
            "DELETE FROM model_api_keys WHERE api_key_id NOT IN (SELECT id FROM api_keys)",
            # v10: 精细化计费 —— 模型缓存价 + 请求日志缓存 token
            "ALTER TABLE models ADD COLUMN cache_read_input_price REAL DEFAULT 0.0",
            "ALTER TABLE models ADD COLUMN cache_write_input_price REAL DEFAULT 0.0",
            "ALTER TABLE models ADD COLUMN supports_reasoning_effort BOOLEAN DEFAULT NULL",
            "ALTER TABLE request_logs ADD COLUMN cache_read_tokens INTEGER",
            "ALTER TABLE request_logs ADD COLUMN cache_write_tokens INTEGER",
            # v11: 请求日志首字延迟（time-to-first-token），流式请求记录
            "ALTER TABLE request_logs ADD COLUMN ttft_ms INTEGER DEFAULT NULL",
            # v13: 归档瘦身标记（详细内容已归档，统计元数据保留在行内）
            "ALTER TABLE request_logs ADD COLUMN archived_at DATETIME DEFAULT NULL",
            # v14: 上下文估算校准（预检估算值落日志 + 上游超限学习的观察窗口）
            "ALTER TABLE request_logs ADD COLUMN est_prompt_tokens INTEGER DEFAULT NULL",
            "ALTER TABLE models ADD COLUMN observed_context_limit INTEGER DEFAULT NULL",
            # v15: 元数据来源分层（manual > provider > public/openrouter > default）
            "ALTER TABLE models ADD COLUMN context_source VARCHAR(50) DEFAULT ''",
            "ALTER TABLE models ADD COLUMN capability_source VARCHAR(50) DEFAULT ''",
            # v12: 累计统计表增加首字延迟累计列（平均首字延迟跨归档保留）
            "ALTER TABLE analytics_cumulative ADD COLUMN sum_ttft_ms INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE analytics_cumulative ADD COLUMN ttft_count INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass
        # v0.2 单行配置
        await conn.execute(text(
            "INSERT OR IGNORE INTO routing_weights (id, w_speed, w_intel, w_stab) "
            "VALUES (1, 0.30, 0.50, 0.20)"
        ))
        await conn.execute(text("INSERT OR IGNORE INTO routing_pin (id) VALUES (1)"))
        # v11: 累计统计数据单行（归档后统计仍保留，重置按钮清零）
        # 显式带全字段 + updated_at，兼容旧表结构（列无 SQL DEFAULT 时裸 INSERT 会 NOT NULL 失败被吞）
        await conn.execute(text(
            "INSERT OR IGNORE INTO analytics_cumulative "
            "(id, total_requests, success_count, auto_requests, total_input_tokens, "
            "total_output_tokens, sum_latency_ms, latency_count, sum_ttft_ms, ttft_count, updated_at) "
            "VALUES (1, 0, 0, 0, 0, 0, 0, 0, 0, 0, CURRENT_TIMESTAMP)"
        ))
        # v0.2 智力种子（避免重复灌）
        # 标记为 source='manual'：原始手工校准基线，Arena 同步绝不覆盖
        for pattern, score, tier, notes in INTEL_SEEDS:
            await conn.execute(text(
                "INSERT OR IGNORE INTO intelligence_static (pattern, score, tier, notes, source) "
                "VALUES (:p, :s, :t, :n, 'manual')"
            ), {"p": pattern, "s": score, "t": tier, "n": notes})
        # 索引（SQLite CREATE INDEX IF NOT EXISTS）
        for sql in [
            "CREATE INDEX IF NOT EXISTS idx_request_logs_model_time ON request_logs(routed_model, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_request_logs_status_time ON request_logs(status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_request_logs_conv_time ON request_logs(conversation_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_request_logs_hc_time ON request_logs(is_health_check, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_audit_time ON admin_audit_log(created_at)",
            # v8.1: rate_limits 唯一约束，防止并发创建产生重复行（根治 MultipleResultsFound）
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limits_model_key ON rate_limits(model_id, key_id)",
            # v3.6: 消息级去重引用列（log_msg_blobs 表由 create_all 自动建）
            "ALTER TABLE request_logs ADD COLUMN request_env_hash VARCHAR(64) DEFAULT NULL",
            "ALTER TABLE request_logs ADD COLUMN request_msg_hashes TEXT DEFAULT NULL",
            "ALTER TABLE request_logs ADD COLUMN response_body_hash VARCHAR(64) DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_log_msg_blobs_hash ON log_msg_blobs(hash)",
        ]:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass
create_tables = init_db
