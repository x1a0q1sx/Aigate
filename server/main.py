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

    # 定时模型刷新（config.yaml model_refresh.scheduled_enabled，默认关；
    # 每次刷新增落 model_refresh_logs，分析页日志类型=模型刷新可查详情）
    mrc = getattr(config, "model_refresh", None)
    if mrc and getattr(mrc, "scheduled_enabled", False) and int(getattr(mrc, "interval_minutes", 0) or 0) > 0:
        async def _run_model_refresh():
            try:
                from .api.admin_router import refresh_models as _rm
                async with AsyncSessionLocal() as db:
                    r = await _rm(None, "scheduled", db)
                print(f"✓ 定时模型刷新: 新增 {r.added} · 删除 {r.removed}"
                      f" · 定价 {r.pricing_updated}（详情见分析页·模型刷新）")
            except Exception as e:
                print(f"⚠️ 定时模型刷新失败: {e}")
        _archive_scheduler.add_job(
            _run_model_refresh, "interval",
            minutes=int(mrc.interval_minutes), id="model_refresh_scheduled",
        )
        print(f"✓ 定时模型刷新已排程（每 {mrc.interval_minutes} 分钟，"
              f"覆盖未单独设频的启用服务商）")

    # v4.2 按服务商的定时模型刷新：服务商编辑页勾选"定时刷新"并自定义频率后，
    # 由每分钟 tick 扫到点者执行（这些服务商从上面全局批量中排除，互不重复刷）。
    async def _run_model_refresh_tick():
        # 重入保护：一次刷新（尤其免费层定价抓取）可能跨分钟，避免同一 tick 叠加
        if getattr(_run_model_refresh_tick, "_busy", False):
            return
        _run_model_refresh_tick._busy = True
        try:
            from datetime import datetime, timedelta
            from sqlalchemy import or_, select
            from .models.provider import Provider
            from .api.admin_router import refresh_models as _rm
            now = datetime.utcnow()
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(Provider).where(
                        Provider.enabled.is_(True),
                        Provider.model_refresh_enabled.is_(True),
                        or_(Provider.model_refresh_next_at.is_(None),
                            Provider.model_refresh_next_at <= now),
                    ).order_by(Provider.id)
                )).scalars().all()
                due = [(p.id, p.name,
                        max(5, min(43200, int(p.model_refresh_interval_minutes or 60))))
                       for p in rows]
            for pid, pname, mins in due:
                # 先拨钟再执行：刷新慢/失败也不会在下个 tick 被重复排队
                async with AsyncSessionLocal() as db:
                    p = await db.get(Provider, pid)
                    if p is not None:
                        p.model_refresh_last_at = datetime.utcnow()
                        p.model_refresh_next_at = datetime.utcnow() + timedelta(minutes=mins)
                        await db.commit()
            # 2026-09: 到点的多个服务商并发刷新（此前逐个等待，一个卡死会拖垮整轮 tick）
            if due:
                from .api.admin_router import refresh_providers_concurrent
                _mrc = getattr(get_config(), "model_refresh", None)
                _provs = []
                for pid, pname, mins in due:
                    async with AsyncSessionLocal() as db:
                        _p = await db.get(Provider, pid)
                        if _p is not None:
                            _provs.append(_p)
                if _provs:
                    _res, _errs = await refresh_providers_concurrent(
                        _provs, trigger="scheduled",
                        concurrency=max(1, int(getattr(_mrc, "concurrency", 6) or 6)),
                        provider_timeout=max(5, int(getattr(_mrc, "provider_timeout_seconds", 45) or 45)),
                    )
                    _name_by_id = {p.id: p.name for p in _provs}
                    for _pid, _r in _res.items():
                        print(f"✓ 定时模型刷新[{_name_by_id.get(_pid, _pid)}]: "
                              f"新增 {_r.get('added', 0)} · 删除 {_r.get('removed', 0)}"
                              f" · 定价 {_r.get('pricing_updated', 0)}（详情见分析页·模型刷新）")
                    for _pid, _msg in _errs.items():
                        print(f"⚠️ 定时模型刷新[{_name_by_id.get(_pid, _pid)}]失败: {_msg}")
        except Exception as e:
            print(f"⚠️ 定时模型刷新 tick 失败: {e}")
        finally:
            _run_model_refresh_tick._busy = False
    _archive_scheduler.add_job(
        _run_model_refresh_tick, "interval",
        minutes=1, id="model_refresh_per_provider", coalesce=True, max_instances=1,
    )

    # 每日签到（领取上游免费积分/额度）：默认 10:30 北京时间——Qoder 活动每日
    # 10:00（UTC+8）刷新，留 30 分钟余量。协议与实测见 core/checkin.py。
    _register_checkin_job()

    _archive_scheduler.start()


async def _run_checkin_daily(trigger: str = "scheduled"):
    """每日签到执行体（定时 / 启动补签共用）。"""
    if getattr(_run_checkin_daily, "_busy", False):
        return
    _run_checkin_daily._busy = True
    try:
        from .core.checkin import collect_targets, run_checkin_batch
        async with AsyncSessionLocal() as db:
            targets, skipped = await collect_targets(db)
            if not targets:
                print(f"✓ 每日签到[{trigger}]：无需签到（跳过 {len(skipped)} 个账号）")
                return
            results = await run_checkin_batch(targets, trigger=trigger, db=db)
        claimed = [r for r in results if r["kind"] == "claimed"]
        already = [r for r in results if r["kind"] == "already_claimed"]
        failed = [r for r in results if r["kind"] == "failed"]
        credit = sum(r["credit"] or 0 for r in claimed)
        print(f"✓ 每日签到[{trigger}]：新领 {len(claimed)}（+{credit:g} 积分）"
              f" · 已领 {len(already)} · 失败 {len(failed)}")
        for r in failed:
            print(f"⚠️ 签到失败[{r['provider_code']}/{r['owner']}]: {r['message']}"
                  + (f"（{r['error']}）" if r.get("error") else ""))
    except Exception as e:
        print(f"⚠️ 每日签到[{trigger}]失败: {e}")
    finally:
        _run_checkin_daily._busy = False


def _register_checkin_job():
    """按配置注册/重排每日签到定时任务（配置保存后也调用，即时生效）。"""
    if _archive_scheduler is None:
        return
    try:
        _archive_scheduler.remove_job("checkin_daily")
    except Exception:
        pass  # 尚未注册
    cc = getattr(config, "checkin", None)
    if not cc or not getattr(cc, "enabled", False):
        print("⏭️ 每日签到未启用（checkin.enabled=false）")
        return
    hour = max(0, min(23, int(getattr(cc, "hour", 10) or 0)))
    minute = max(0, min(59, int(getattr(cc, "minute", 30) or 0)))
    _archive_scheduler.add_job(
        _run_checkin_daily, "cron",
        hour=hour, minute=minute,
        timezone="Asia/Shanghai",   # 显式时区：上游按 UTC+8 刷新，不依赖服务器 TZ
        id="checkin_daily", coalesce=True, max_instances=1,
    )
    print(f"✓ 每日签到已排程（每天 {hour:02d}:{minute:02d} 北京时间）")


def reschedule_checkin_job():
    """配置变更后重排（供 admin 端点在 save_config 后调用）。"""
    _register_checkin_job()


async def _checkin_startup_catchup():
    """启动补签：服务重启错过时间点后，当天仍自动补签（同类项目的标准做法）。

    仅当「已过今天的签到时刻」且「当天仍有未完成账号」时才跑；
    collect_targets 已内置幂等（今日已完成的账号会被跳过），故重复调用无副作用。
    """
    try:
        cc = getattr(config, "checkin", None)
        if not cc or not getattr(cc, "enabled", False):
            return
        if not getattr(cc, "startup_catchup", True):
            return
        from datetime import datetime, timedelta, timezone
        cst = timezone(timedelta(hours=8))
        now = datetime.now(cst)
        target = now.replace(hour=max(0, min(23, int(getattr(cc, "hour", 10) or 0))),
                             minute=max(0, min(59, int(getattr(cc, "minute", 30) or 0))),
                             second=0, microsecond=0)
        if now < target:
            return  # 今天还没到点，交给定时任务
        await _run_checkin_daily("startup_catchup")
    except Exception as e:
        print(f"⚠️ 启动补签失败: {e}")
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
    # 存量 OAuth 连接回填服务商（自动登记上线前的旧连接补建，幂等）
    import asyncio as _aio

    async def _backfill_oauth_providers():
        try:
            from server.core.oauth_client import get_oauth_client
            from server.db import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                conns = await get_oauth_client().list_connections(db)
            codes = {c["provider_code"] for c in conns}
            for code in codes:
                try:
                    async with AsyncSessionLocal() as db:
                        await get_oauth_client()._ensure_provider_registered(db, code)
                except Exception as e:
                    logger.warning("oauth provider backfill %s failed: %s", code, e)
            if codes:
                print(f"✓ OAuth 存量连接回填服务商完成（{len(codes)} 家）")
        except Exception as e:
            logger.warning("oauth provider backfill failed: %s", e)
    _backfill_task = _aio.ensure_future(_backfill_oauth_providers())
    # u1s1 客户端证明（x-u1s1-attestation）启动预热：推理请求缺它必 403「非 u1s1 客户端」。
    # 进程级缓存冷启动时首个 combo/auto 请求要现拉（阻塞 ≤4s，慢则冷却 30s 让整条链的 u1s1
    # 候选全 403）。启动即后台拉一次填缓存（有效期 7 天，临期自动续），把冷启动窗口消掉。
    async def _warm_u1s1_attestation():
        try:
            from sqlalchemy import select as _sel
            from server.db import AsyncSessionLocal
            from server.models.provider import Provider as _P
            async with AsyncSessionLocal() as db:
                hit = (await db.execute(
                    _sel(_P.id).where(_P.credential_type == "oauth",
                                      _P.oauth_code == "u1s1",
                                      _P.enabled.is_(True)).limit(1)
                )).first()
            if not hit:
                return
            from server.core.u1s1_attestation import get_attestation
            att = await get_attestation("https://api.u1s1.io/v1")
            print("✓ u1s1 attestation 预热完成" if att else "⚠️ u1s1 attestation 预热未获 token（首次推理会重试）")
        except Exception as e:
            logger.warning("u1s1 attestation 预热失败: %s", e)
    _warm_att_task = _aio.ensure_future(_warm_u1s1_attestation())
    # 每日签到启动补签：服务重启错过签到时间点时，当天自动补签
    #（collect_targets 内置幂等——今日已完成的账号会跳过，重复调用无副作用）
    _checkin_task = _aio.ensure_future(_checkin_startup_catchup())
    # OpenCode 免费层 CLI sidecar 守护：上游只认「官方 CLI 会话」，AIGate 经其 HTTP API
    # 转发（见 core/opencode_sidecar.py）。sidecar 崩了本任务自动拉起；未安装 CLI 时
    # 静默跳过（该免费候选自然不可用，不影响其它路由）。
    # 配置项（超时/agent/端口/是否自启）见 config.yaml 的 opencode_bridge 段，可热改。
    async def _guard_opencode_sidecar():
        import shutil as _shutil
        from server.core import opencode_sidecar as _sc

        def _flag(name: str, default: bool) -> bool:
            try:
                c = getattr(config, "opencode_bridge", None)
                v = getattr(c, name, None) if c is not None else None
                return default if v is None else bool(v)
            except Exception:
                return default

        first = True
        while True:
            try:
                # ① 保证 CLI 侧 agent 定义正确（幂等；这是「工具权限挂起致超时」的根治点）
                if _flag("manage_agent_config", True):
                    ok, note = _sc.ensure_bridge_agent_config()
                    if first:
                        print(("✓ OpenCode CLI agent：" if ok else "⚠️ OpenCode CLI agent：") + note)
                # ② sidecar 探活 / 拉起
                if not await _sc.sidecar_alive():
                    if not _flag("auto_start", True):
                        if first:
                            print("⏸️ OpenCode sidecar 未运行（opencode_bridge.auto_start=false，不自动拉起）")
                    else:
                        # CLI 位置：配置 bin_path > 环境变量 > ~/opencode/bin/opencode > PATH
                        # （不硬编码绝对路径：换机/换用户目录都能工作，也避免提交钩子拦路径）
                        bin_path = ""
                        try:
                            bin_path = (getattr(config.opencode_bridge, "bin_path", "") or "").strip()
                        except Exception:
                            bin_path = ""
                        bin_path = bin_path or os.environ.get("AIGATE_OPENCODE_BIN") or os.path.join(
                            os.path.expanduser("~"), "opencode", "bin", "opencode")
                        try:
                            port = str(getattr(config.opencode_bridge, "port", 4096))
                        except Exception:
                            port = os.environ.get("AIGATE_OPENCODE_PORT", "4096")
                        exe = bin_path if os.path.exists(bin_path) else _shutil.which("opencode")
                        if exe:
                            if first:
                                print(f"… OpenCode sidecar 未运行，尝试启动：{exe}")
                            _aio.create_subprocess_exec(
                                exe, "serve", "--port", port, "--hostname", "127.0.0.1",
                                stdout=_aio.subprocess.DEVNULL, stderr=_aio.subprocess.DEVNULL,
                                start_new_session=True)
                            await _aio.sleep(8)
                            alive = await _sc.sidecar_alive()
                            print("✓ OpenCode sidecar 已就绪" if alive
                                  else "⚠️ OpenCode sidecar 启动后仍不可用（该免费候选将跳过）")
                        elif first:
                            print("⏭️ 未发现 opencode CLI，跳过 sidecar（见 docs/findings-opencode-free-tier.md）")
                elif first:
                    print("✓ OpenCode sidecar 已在运行（CLI 桥接就绪）")
            except Exception as e:
                logger.warning("opencode sidecar 守护异常: %s", e)
            first = False
            await _aio.sleep(120)
    _sidecar_guard_task = _aio.ensure_future(_guard_opencode_sidecar())
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
# Combo 前缀路由（最后添加 = 最外层）：/combo:<名称或id>/v1/... → 剥前缀
# 转发进既有 /v1 端点，请求强制走该组合；详见 server/core/combo_prefix.py
from server.core.combo_prefix import ComboPrefixMiddleware
app.add_middleware(ComboPrefixMiddleware)
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
from .api.checkin_router import router as checkin_router
app.include_router(checkin_router)     # 每日签到：/admin/api/checkin/*（一键签到 + 额度监控）
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
