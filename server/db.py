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
from .models.health_check import HealthCheck
from .models.rate_limit import RateLimitState
from .models.request_log import RequestLog
from .models.intelligence import IntelligenceStatic
from .models.routing_config import RoutingWeights, RoutingPin, AdminAuditLog
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
    pool_size=10,       # 连接池大小，适应多请求并发
    max_overflow=5,     # 超出 pool_size 时可额外创建的数量
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
        await conn.run_sync(Base.metadata.create_all)
        # v1/v2 增量迁移
        for sql in [
            "ALTER TABLE models ADD COLUMN priority_boost INTEGER DEFAULT 0",
            "ALTER TABLE models ADD COLUMN auto_excluded BOOLEAN DEFAULT 0",
            "ALTER TABLE models ADD COLUMN manual_cooldown_until TIMESTAMP",
            "ALTER TABLE models ADD COLUMN success_rate REAL",
            "ALTER TABLE models ADD COLUMN avg_latency_ms REAL",
            "ALTER TABLE models ADD COLUMN avg_ttft_ms REAL",
            "ALTER TABLE models ADD COLUMN avg_tps REAL",
            "ALTER TABLE models ADD COLUMN pricing_source VARCHAR(500) DEFAULT ''",
            "ALTER TABLE models ADD COLUMN pricing_updated_at TIMESTAMP",
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
        # v0.2 智力种子（避免重复灌）
        for pattern, score, tier, notes in INTEL_SEEDS:
            await conn.execute(text(
                "INSERT OR IGNORE INTO intelligence_static (pattern, score, tier, notes) "
                "VALUES (:p, :s, :t, :n)"
            ), {"p": pattern, "s": score, "t": tier, "n": notes})
        # 索引（SQLite CREATE INDEX IF NOT EXISTS）
        for sql in [
            "CREATE INDEX IF NOT EXISTS idx_request_logs_model_time ON request_logs(routed_model, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_request_logs_status_time ON request_logs(status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_request_logs_conv_time ON request_logs(conversation_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_audit_time ON admin_audit_log(created_at)",
        ]:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass
create_tables = init_db
