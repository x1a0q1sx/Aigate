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
from .api.anthropic_router import router as anthropic_router  # Anthropic Messages API
from .api.responses_router import router as responses_router  # Responses API (Codex CLI)
from .api.combos_router import router as combos_router  # Combos 组合 CRUD
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .api.media_router import router as media_router  # 配额/代理池/媒体生成
from .api.oauth_router import router as oauth_router  # OAuth 接入（复用 9Router client_id）
from .api.auth_router import router as auth_router  # 管理面板登录认证
from .api.update_router import router as update_router  # 一键更新（检查/执行/状态）
from .api.route_decisions_router import router as route_decisions_router
from .core.auth import AuthMiddleware
config = get_config()
# 全局单例
_health_checker: HealthChecker = None
def get_health_checker() -> HealthChecker:
    global _health_checker
    return _health_checker


# 每日维护调度器（归档/备份/周评分同步共用，与健康探测器解耦）
_archive_scheduler = None
# 后台智力评分同步任务（启动时不阻塞，关闭时取消）
_intel_sync_task = None
def _schedule_maintenance():
    """每日维护调度器：日志归档（02:00）+ 数据库备份（config.backup）+ 每周智力评分同步"""
    global _archive_scheduler
    _archive_scheduler = AsyncIOScheduler()

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

    _archive_scheduler.add_job(_run_archive, "cron", hour=2, minute=0, id="log_archive_daily")

    ac = config.log_archive
    if ac.enabled:
        print(f"✓ 每日归档已排程（每天 02:00 归档昨日日志）")

    # C3: 数据库定时备份
    bc = getattr(config, "backup", None)
    if bc and bc.enabled:
        async def _run_backup():
            try:
                from .core.backup_service import run_backup
                r = await run_backup("scheduled")
                if r.get("ok"):
                    print(f"✓ 每日备份: {r['file']} ({r['size'] // 1024} KB)"
                          + (f"，清理 {len(r['pruned'])} 份旧备份" if r.get("pruned") else ""))
                else:
                    print(f"⚠️ 每日备份失败: {r.get('error')}")
            except Exception as e:
                print(f"⚠️ 每日备份失败: {e}")
        _archive_scheduler.add_job(
            _run_backup, "cron", hour=bc.hour, minute=bc.minute, id="db_backup_daily",
        )
        print(f"✓ 数据库每日备份已排程（每天 {bc.hour:02d}:{bc.minute:02d}，保留 {bc.keep} 份）")

    # E3: 每周智力评分自动同步（周一凌晨 5 点）
    if getattr(config.arena, "sync_weekly", False):
        async def _run_intel_sync():
            try:
                from server.core.intelligence_sync import sync_intelligence
                async with AsyncSessionLocal() as db:
                    n = await sync_intelligence(db)
                print(f"✓ 每周智力评分同步完成: {n} 条")
            except Exception as e:
                print(f"⚠️ 每周智力评分同步失败: {e}")
        _archive_scheduler.add_job(
            _run_intel_sync, "cron", day_of_week="mon", hour=5, minute=0, id="intel_sync_weekly",
        )
        print("✓ 智力评分每周自动同步已排程（周一 05:00）")

    _archive_scheduler.start()
# 内置服务商模板，首次启动（空数据库）自动创建。
# 仅保留 3 个开箱即用的免费/直连渠道，其余由用户自行添加。
BUILTIN_PROVIDERS = [
    {"name": "MiMo", "base_url": "https://api.xiaomimimo.com", "api_type": "openai_compat",
     "credential_type": "free_tier", "oauth_code": "mimo-free",
     "models": [{"model_id": "mimo-auto", "display_name": "MiMo Auto", "is_free": True, "auto_enabled": True}]},
    {"name": "OpenCode", "base_url": "https://opencode.ai", "api_type": "openai_compat",
     "credential_type": "free_tier", "oauth_code": "opencode",
     "models": [{"model_id": "claude-sonnet-4"}, {"model_id": "gpt-5.5"}, {"model_id": "gemini-3-flash"}]},
    {"name": "AtomCode", "base_url": "https://llm-api.atomgit.com/v1", "api_type": "atomcode",
     "credential_type": "atomcode",
     "models": [{"model_id": "GLM-5.1"}, {"model_id": "Qwen/Qwen3-VL-8B-Instruct"},
                {"model_id": "Qwen/Qwen3.6-35B-A3B"}, {"model_id": "deepseek-v4-flash"}]},
]
async def init_builtin_providers():
    """首次启动（空数据库）创建内置服务商模板 + 已知模型。
    仅保留 3 个开箱即用的免费/直连渠道及其模型，其余由用户自行添加。"""
    from sqlalchemy import select
    from server.models.provider import Provider
    from server.models.model import Model
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Provider))
        existing = list(result.scalars().all())
        if len(existing) == 0:
            # 数据库空，创建内置模板 + 已知模型
            for p in BUILTIN_PROVIDERS:
                models = p.get("models", [])
                provider_fields = {k: v for k, v in p.items() if k != "models"}
                provider = Provider(**provider_fields)
                session.add(provider)
                await session.flush()
                for m in models:
                    session.add(Model(
                        provider_id=provider.id, model_id=m["model_id"],
                        display_name=m.get("display_name", m["model_id"]),
                        is_free=m.get("is_free", False),
                        auto_enabled=m.get("auto_enabled", False),
                        enabled=True, supports_streaming=True,
                    ))
            await session.commit()
            print(f"✓ 已创建 {len(BUILTIN_PROVIDERS)} 个内置服务商模板及其模型")
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 初始化
    print("\n🚀 启动 AIGate 智能 LLM 聚合网关...")
    # 创建数据库表
    await create_tables()
    print("✓ 数据库初始化完成")
    # 初始化 HTTP 代理池
    from server.core.proxy_pool import init_proxy_pool
    init_proxy_pool(config.proxy_pool.model_dump() if hasattr(config.proxy_pool, "model_dump") else {})
    if config.proxy_pool.enabled:
        print(f"✓ HTTP 代理池已启用 ({config.proxy_pool.strategy}, {len(config.proxy_pool.proxies)} 代理)")
    # 创建内置服务商模板
    await init_builtin_providers()
    # 健康检查器实例：仅用于「真实请求失败冷却」+「手动测速」，
    # 不再启动定时自动探测（避免白费 token）。
    global _health_checker
    _health_checker = HealthChecker()
    print("✓ 健康检查器已初始化（自动探测已关闭，速度/健康数据来自真实调用日志）")
    # 每日维护调度器（日志归档 / 数据库备份 / 每周智力评分同步）
    _schedule_maintenance()
    # 启动 OAuth token 主动刷新调度器
    from server.api.oauth_router import start_oauth_refresh_scheduler
    start_oauth_refresh_scheduler()
    print("✓ OAuth token 主动刷新调度器已启动（60s 扫一次）")
    # P0-3: 日志写入队列（请求路径零落库，后台批量 commit + WAL 周期 checkpoint）
    from server.core.log_queue import start_log_queue
    start_log_queue()
    print("✓ 日志写入队列已启动（批量落库，请求路径零等待）")
    # 同步智力评分（从 Arena AI 排行榜）—— 后台执行，不阻塞启动
    global _intel_sync_task
    if config.arena.sync_on_startup:
        try:
            from server.core.intelligence_sync import start_intelligence_sync
            _intel_sync_task = start_intelligence_sync()
            print("✓ 智力评分同步已在后台启动（不阻塞启动）")
        except Exception as e:
            logger.warning("智力评分后台同步启动失败: %s", e)
    else:
        print("⏭️ 已跳过 Arena 智力评分同步（arena.sync_on_startup=false）")
    # C4: 启动自检（版本 + 关键资源盘点，异常项醒目提示）
    try:
        from .core.selfcheck import get_version_info, run_selfcheck
        sc = await run_selfcheck()
        vi = get_version_info()
        summary = " ".join(f"{i['name']}={i['detail']}" for i in sc["items"])
        print(f"✓ 自检 v{vi['version']} ({vi['database']}): {summary}")
        bad = [i["name"] for i in sc["items"] if not i["ok"]]
        if bad:
            print(f"⚠️ 自检异常项: {', '.join(bad)}")
    except Exception as e:
        print(f"⚠️ 启动自检失败: {e}")
    print(f"✓ AIGate 就绪")
    yield
    # 关闭
    global _archive_scheduler
    if _archive_scheduler is not None:
        _archive_scheduler.shutdown(wait=False)
    # 取消可能仍在进行的后台智力评分同步，避免 engine.dispose 后回调
    if _intel_sync_task is not None and not _intel_sync_task.done():
        _intel_sync_task.cancel()
    # P0-3: flush 日志队列剩余内容后再释放 engine
    try:
        from server.core.log_queue import stop_log_queue
        await stop_log_queue()
    except Exception as e:
        logger.warning("log queue shutdown: %s", e)
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
# 认证中间件（/admin/* 需要登录，/v1/* 保持开放）
app.add_middleware(AuthMiddleware)
# ============================================================
# 注册 API 路由（必须在 SPA 回退之前注册，确保 API 优先匹配）
# ============================================================
app.include_router(auth_router)  # 认证端点（login/logout/check/password）
app.include_router(v1_router)
app.include_router(admin_router)
app.include_router(admin_routing_router)  # v0.2: 排行/日志/干预/审计
app.include_router(anthropic_router)  # Anthropic Messages API 兼容入口
app.include_router(responses_router)  # Responses API 兼容入口（Codex CLI）
app.include_router(combos_router)   # Combos 组合 CRUD（/admin/api/combos）
app.include_router(media_router)   # 配额追踪 + 代理池 + 媒体生成
app.include_router(oauth_router)   # OAuth 接入：/admin/oauth/*
app.include_router(update_router)  # 一键更新：检查/执行/状态
app.include_router(route_decisions_router)  # 路由决策中心：候选评分与 fallback 链
from .api.admin_ext_router import router as admin_ext_router
app.include_router(admin_ext_router)   # D1 网关密钥 / E1 别名 / A4 缓存管理
from .api.passthrough_router import router as passthrough_router
app.include_router(passthrough_router)  # A3: /v1/embeddings、/v1/images/generations
from .api.gemini_router import router as gemini_router
app.include_router(gemini_router)      # A1: /v1beta/* Gemini 原生协议
from .api.admin_ops_router import router as admin_ops_router
app.include_router(admin_ops_router)   # B1/B2/B4/B5/D3/D4: 诊断/导出/失败看板/实时监控/通知/价格健康
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
    SPA_PATHS = ["/dashboard", "/providers", "/models", "/health", "/monitor", "/auto", "/route-decisions", "/combos", "/oauth", "/proxies", "/media", "/analytics", "/playground", "/token-saver", "/settings", "/keys", "/aliases", "/admin", "/login"]
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
