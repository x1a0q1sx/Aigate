"""
AIGate 主入口
智能 LLM 聚合网关
"""
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .config import get_config
from .db import create_tables, engine, AsyncSessionLocal
from .core.key_manager import KeyManager
from .core.health_checker import HealthChecker
from .core.crypto_service import get_crypto_service
from .api.v1_router import router as v1_router
from .api.admin_router import router as admin_router
from .api.admin_routing import router as admin_routing_router  # v0.2
config = get_config()
# 全局单例
_health_checker: HealthChecker = None
def get_health_checker() -> HealthChecker:
    global _health_checker
    return _health_checker


def _schedule_log_archive(archive_config):
    """每日定时归档：每天凌晨 2 点自动归档昨天的日志"""
    if _health_checker is None or _health_checker._scheduler is None:
        return

    async def _run_archive():
        try:
            from .api.admin_routing import _do_archive
            async with AsyncSessionLocal() as db:
                result = await _do_archive(db)  # 默认归档昨天
                c = result.get("archived_count", 0)
                if c > 0:
                    print(f"✓ 每日归档: {c} 条 → {result.get('filename', '?')}")
        except Exception as e:
            print(f"⚠️ 每日归档失败: {e}")

    _health_checker._scheduler.add_job(
        _run_archive,
        "cron",
        hour=2,
        minute=0,
        id="log_archive_daily",
    )
    print(f"✓ 每日归档调度器已启动（每天 02:00 归档昨日日志）")
# 内置服务商模板，首次启动自动创建
BUILTIN_PROVIDERS = [
    {"name": "OpenAI", "base_url": "https://api.openai.com", "api_type": "openai_compat"},
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "api_type": "openai_compat"},
    {"name": "Groq", "base_url": "https://api.groq.com/openai", "api_type": "openai_compat"},
    {"name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode", "api_type": "openai_compat"},
    {"name": "智谱AI", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_type": "openai_compat"},
    {"name": "Moonshot", "base_url": "https://api.moonshot.cn", "api_type": "openai_compat"},
    {"name": "SiliconFlow", "base_url": "https://api.siliconflow.cn", "api_type": "openai_compat"},
    {"name": "Together", "base_url": "https://api.together.xyz", "api_type": "openai_compat"},
    {"name": "Fireworks", "base_url": "https://api.fireworks.ai/inference", "api_type": "openai_compat"},
    {"name": "Anthropic", "base_url": "https://api.anthropic.com", "api_type": "anthropic"},
]
async def init_builtin_providers():
    """首次启动创建内置服务商模板"""
    from sqlalchemy import select
    from server.models.provider import Provider
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Provider))
        existing = list(result.scalars().all())
        if len(existing) == 0:
            # 数据库空，创建内置模板
            for p in BUILTIN_PROVIDERS:
                provider = Provider(**p)
                session.add(provider)
            await session.commit()
            print(f"✓ 已创建 {len(BUILTIN_PROVIDERS)} 个内置服务商模板")
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 初始化
    print("\n🚀 启动 AIGate 智能 LLM 聚合网关...")
    # 创建数据库表
    await create_tables()
    print("✓ 数据库初始化完成")
    # 创建内置服务商模板
    await init_builtin_providers()
    # 启动健康探测调度器
    global _health_checker
    _health_checker = HealthChecker()
    key_manager = KeyManager(get_crypto_service())
    await _health_checker.start_scheduler(AsyncSessionLocal, key_manager)
    print(f"✓ 健康探测调度器已启动（每 {config.health_check.interval_minutes} 分钟）")
    # 启动日志归档调度器
    ac = config.log_archive
    if ac.enabled:
        _schedule_log_archive(ac)
    # 同步智力评分（从 Arena AI 排行榜）
    try:
        from server.core.intelligence_sync import sync_intelligence
        async with AsyncSessionLocal() as sync_db:
            n = await sync_intelligence(sync_db)
        if n:
            print(f"✓ 智力评分已同步: {n} 个模型")
    except Exception as e:
        print(f"⚠️ 智力评分同步失败: {e}")
    print(f"✓ AIGate 就绪")
    yield
    # 关闭
    if _health_checker:
        _health_checker.stop_scheduler()
    await engine.dispose()
    print("\n✓ AIGate 已关闭")
# 创建 FastAPI 应用
app = FastAPI(
    title="AIGate",
    description="智能 LLM 聚合网关 - 自定义服务商 + Auto 智能路由",
    version="1.0.0",
    lifespan=lifespan
)
# CORS
if config.server.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.server.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
# ============================================================
# 注册 API 路由（必须在 SPA 回退之前注册，确保 API 优先匹配）
# ============================================================
app.include_router(v1_router)
app.include_router(admin_router)
app.include_router(admin_routing_router)  # v0.2: 排行/日志/干预/审计
# ============================================================
# 挂载前端静态文件 & SPA 路由回退
# ============================================================
_client_dist = Path(__file__).resolve().parent.parent / "client" / "dist"
logger = logging.getLogger(__name__)
logger.debug("client_dist = %s", _client_dist)
logger.debug("client_dist exists = %s", _client_dist.exists())
if _client_dist.exists():
    # 挂载静态资源到根路径（index.html 中引用 /assets/... 和 /vite.svg）
    app.mount("/assets", StaticFiles(directory=str(_client_dist / "assets")), name="assets")
    from fastapi.responses import FileResponse
    # vite.svg favicon
    @app.get("/vite.svg")
    async def vite_svg():
        return FileResponse(str(_client_dist / "vite.svg"))
    # SPA 路由回退：为每个前端路由注册处理器
    # 前端 Vue Router 路由: /dashboard, /providers, /models, /health, /auto, /analytics, /playground
    SPA_PATHS = ["/dashboard", "/providers", "/models", "/health", "/auto", "/analytics", "/playground", "/admin"]
    for spa_path in SPA_PATHS:
        # 精确匹配
        def _make_handler():
            async def handler():
                return FileResponse(str(_client_dist / "index.html"))
            return handler
        app.get(spa_path)(_make_handler())
        # 子路径匹配（如 /providers/xxx）
        app.get(f"{spa_path}/{{full_path:path}}")(_make_handler())
    logger.debug("前端 SPA 路由已挂载: %s", SPA_PATHS)
else:
    logger.debug("前端未构建，使用开发模式")
    @app.get("/admin")
    async def admin_dev():
        return {
            "message": "AIGate Admin UI not built yet",
            "build_instructions": "cd client && npm install && npm run build"
        }
@app.get("/")
async def root():
    return {
        "name": "AIGate",
        "description": "智能 LLM 聚合网关",
        "endpoints": {
            "openai_api": "/v1",
            "admin_ui": "/admin",
            "admin_api": "/admin/api"
        }
    }
