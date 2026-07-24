"""
AIGate 发布前清理脚本
一键清除所有用户数据：数据库、归档、运行期日志、pytest 缓存、Python 缓存
已擦除 config.yaml 中的密钥与代理（端口、日志归档等保留，密钥启动后自动重新生成）
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 要清理的路径（注意：数据库不再整库删除，而是在 ensure_builtin_providers() 中
# 裁剪保留 3 个内置 Free Tier 服务商及其模型，清空其余用户数据）
CLEAN_PATHS = [
    ROOT / "data" / "archives",         # 日志归档目录
    ROOT / "__pycache__",               # Python 缓存
]


def clean():
    print("🧹 AIGate 发布前清理")
    print(f"   项目目录: {ROOT}")
    print()

    deleted = 0
    errors = 0

    for p in CLEAN_PATHS:
        if not p.exists():
            continue
        try:
            if p.is_file():
                p.unlink()
                print(f"   ✓ 删除文件: {p.relative_to(ROOT)}")
                deleted += 1
            elif p.is_dir():
                import shutil
                shutil.rmtree(p)
                print(f"   ✓ 删除目录: {p.relative_to(ROOT)}")
                deleted += 1
        except Exception as e:
            print(f"   ✗ 失败: {p.relative_to(ROOT)} — {e}")
            errors += 1

    # 递归清理所有 __pycache__
    for pycache in ROOT.rglob("__pycache__"):
        try:
            import shutil
            shutil.rmtree(pycache)
            print(f"   ✓ 删除缓存: {pycache.relative_to(ROOT)}")
            deleted += 1
        except Exception:
            pass

    # 额外清理：运行期产生的调试日志、pytest 缓存、启动日志
    import glob as _glob
    extra_patterns = [
        str(ROOT / "data" / "*.log"),        # 各 provider 调试日志（backend_*/mimo*/compress*/boot* 等）
        str(ROOT / ".pytest_cache"),         # pytest 运行缓存
        str(ROOT / "gateway_boot.log"),      # 网关启动日志
    ]
    for pat in extra_patterns:
        for match in _glob.glob(pat):
            p = Path(match)
            try:
                if p.is_file():
                    p.unlink()
                    print(f"   ✓ 删除文件: {p.relative_to(ROOT)}")
                    deleted += 1
                elif p.is_dir():
                    import shutil
                    shutil.rmtree(p)
                    print(f"   ✓ 删除目录: {p.relative_to(ROOT)}")
                    deleted += 1
            except Exception as e:
                print(f"   ✗ 失败: {p.relative_to(ROOT)} — {e}")
                errors += 1

    # 裁剪数据库：保留 4 个服务商（MiMo/OpenCode/AtomCode 内置 Free Tier + 魔塔AI）及其模型，
    # 仅清空密钥（ApiKey 表）与其余用户数据；魔塔AI 依赖现有数据库，不在此处新建
    ensure_builtin_providers()

    # 物理压缩数据库：DELETE 只标记空闲页、不清零磁盘，被删的请求日志仍以空闲页
    # 残留在 2GB 的 db/wal 里可恢复。VACUUM 重写库文件只保留存活数据并截断 WAL，
    # 彻底消除数据残留，否则发布副本存在信息泄露风险。
    vacuum_db()

    # 擦除 config.yaml 中的密钥与代理配置（最后执行，确保最终 config 密钥为空、首启重生）
    scrub_config_secrets()

    print()
    if errors:
        print(f"⚠️  完成：{deleted} 项已清理，{errors} 项失败")
    else:
        print(f"✅ 全部清理完成，共 {deleted} 项。4 个服务商（MiMo/OpenCode/AtomCode + 魔塔AI）已保留，密钥已清空，config 密钥已擦除。")
    print("   下次启动时，AIGate 直接以保留的 3 个内置服务商运行（无需重新添加）。")


def scrub_config_secrets():
    """擦除 config.yaml 中的密钥与代理，首次启动由 server/config.py 自动重新生成。
    仅清空敏感字段，保留端口、日志归档等其他配置。"""
    cfg = ROOT / "config.yaml"
    if not cfg.exists():
        print("   • config.yaml 不存在，跳过密钥擦除")
        return
    try:
        import yaml
        with open(cfg, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        changed = False
        # 加密密钥 / AIGate 访问密钥：置空，启动后自动重新生成
        sec = data.setdefault("security", {})
        if sec.get("encryption_key"):
            sec["encryption_key"] = ""
            changed = True
        if sec.get("aigate_api_key"):
            sec["aigate_api_key"] = ""
            changed = True
        # 管理面板密码哈希：置空，启动后用默认密码 aigate123 重建
        auth = data.setdefault("auth", {})
        if auth.get("password_hash"):
            auth["password_hash"] = ""
            changed = True
        # 代理池：清空本地代理并禁用（缺失/禁用均由 config.py 用默认空配置）
        pp = data.get("proxy_pool")
        if pp:
            if pp.get("enabled"):
                pp["enabled"] = False
                changed = True
            if pp.get("proxies"):
                pp["proxies"] = []
                changed = True
        if changed:
            with open(cfg, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            print("   ✓ 已擦除 config.yaml 中的密钥与代理（启动后自动重新生成）")
        else:
            print("   • config.yaml 无敏感字段需擦除")
    except Exception as e:
        print(f"   ✗ 擦除 config.yaml 失败: {e}")


def ensure_builtin_providers():
    """裁剪数据库：保留 3 个内置 Free Tier 服务商（MiMo/OpenCode/AtomCode）及其模型，
    清空其余所有服务商、模型、密钥、日志、Combo 等用户数据。
    若数据库不存在则新建并写入这 3 个服务商 + 已知模型。
    仅依赖项目自身的 SQLAlchemy 会话，不依赖网关是否在运行。"""
    import asyncio
    from sqlalchemy import select, delete
    try:
        from server.db import AsyncSessionLocal, create_tables
        from server.models.provider import Provider
        from server.models.model import Model
        from server.models.api_key import ApiKey
        from server.models.combo import Combo
        from server.models.request_log import RequestLog
        from server.models.oauth_token import OAuthToken
        from server.models.rate_limit import RateLimitState
        from server.models.routing_config import AdminAuditLog
        from server.models.health_check import HealthCheck
    except Exception as e:
        print(f"   ✗ 无法导入服务端模块（请确保在项目根目录运行 clean.py）: {e}")
        return

    # 3 个内置 Free Tier 服务商（使用数据库真实名称，free executor 才能正确解析）
    BUILTINS = [
        {"name": "MiMo", "base_url": "https://api.xiaomimimo.com",
         "api_type": "openai_compat", "credential_type": "free_tier", "oauth_code": "mimo-free",
         "models": [{"model_id": "mimo-auto", "display_name": "MiMo Auto", "is_free": True, "auto_enabled": True}]},
        {"name": "OpenCode", "base_url": "https://opencode.ai",
         "api_type": "openai_compat", "credential_type": "free_tier", "oauth_code": "opencode",
         "models": [{"model_id": "claude-sonnet-4"}, {"model_id": "gpt-5.5"}, {"model_id": "gemini-3-flash"}]},
        {"name": "AtomCode", "base_url": "https://llm-api.atomgit.com/v1",
         "api_type": "atomcode", "credential_type": "atomcode", "oauth_code": None,
         "models": [{"model_id": "GLM-5.1"},
                    {"model_id": "Qwen/Qwen3-VL-8B-Instruct"},
                    {"model_id": "Qwen/Qwen3.6-35B-A3B"},
                    {"model_id": "deepseek-v4-flash"}]},
    ]
    # 保留名单：兼容不同命名（DB 真实名 + main.py BUILTIN_PROVIDERS 名）
    KEEP_NAMES = {"MiMo Code Free", "AtomCode", "MiMo", "OpenCode", "魔塔AI"}

    async def _run():
        await create_tables()
        async with AsyncSessionLocal() as session:
            # 1) 找出要保留的 provider
            res = await session.execute(select(Provider).where(Provider.name.in_(list(KEEP_NAMES))))
            existing = {p.name: p for p in res.scalars().all()}
            by_oauth = {p.oauth_code: p for p in existing.values() if p.oauth_code}
            kept_ids = [p.id for p in existing.values()]

            # 2) 清空用户/运行期数据（保留 intelligence_static / routing_weights / routing_pin 基线）
            for tbl in (RequestLog, AdminAuditLog, HealthCheck, OAuthToken, ApiKey, RateLimitState, Combo):
                await session.execute(delete(tbl))

            # 3) 删除非保留 provider 的模型 + 非保留 provider
            if kept_ids:
                await session.execute(delete(Model).where(Model.provider_id.notin_(kept_ids)))
            else:
                await session.execute(delete(Model))
            await session.execute(delete(Provider).where(Provider.name.notin_(list(KEEP_NAMES))))

            # 4) 确保 3 个内置服务商 + 已知模型存在（已有的不重复创建，只补缺失模型）
            for b in BUILTINS:
                prov = existing.get(b["name"]) or (by_oauth.get(b["oauth_code"]) if b["oauth_code"] else None)
                if prov is None:
                    prov = Provider(
                        name=b["name"], base_url=b["base_url"], api_type=b["api_type"],
                        credential_type=b["credential_type"], oauth_code=b["oauth_code"],
                        description="内置 Free Tier 服务商",
                    )
                    session.add(prov)
                    await session.flush()
                else:
                    # 修正字段，确保 credential_type / oauth_code 正确
                    prov.credential_type = b["credential_type"]
                    prov.oauth_code = b["oauth_code"]
                    prov.base_url = b["base_url"]
                    prov.api_type = b["api_type"]
                for m in b["models"]:
                    exist = await session.execute(
                        select(Model).where(Model.provider_id == prov.id, Model.model_id == m["model_id"]))
                    if exist.scalar_one_or_none() is None:
                        session.add(Model(
                            provider_id=prov.id, model_id=m["model_id"],
                            display_name=m.get("display_name", m["model_id"]),
                            is_free=m.get("is_free", False),
                            auto_enabled=m.get("auto_enabled", False),
                            enabled=True, supports_streaming=True,
                        ))
            await session.commit()

    try:
        asyncio.run(_run())
        print("   ✓ 已保留 4 个服务商（MiMo/OpenCode/AtomCode + 魔塔AI）及其模型，所有密钥已清空，其余用户数据已清空")
    except Exception as e:
        print(f"   ✗ 保留内置服务商失败: {e}")


def vacuum_db():
    """发布前必须：DELETE 仅标记空闲页、不清零磁盘，被删的请求日志（prompt/回复/
    可能泄露的 header）仍以空闲页形式残留在 2GB 的 db/wal 文件里可被恢复。
    VACUUM 重写库文件只保留存活数据，并截断 WAL，彻底消除数据残留。"""
    import sqlite3 as _sqlite
    db_path = ROOT / "data" / "aigate.db"
    if not db_path.exists():
        return
    wal_path = ROOT / "data" / "aigate.db-wal"
    before = db_path.stat().st_size
    wal_before = wal_path.stat().st_size if wal_path.exists() else 0
    try:
        # isolation_level=None 进入 autocommit，VACUUM 不能在事务内执行
        conn = _sqlite.connect(str(db_path), timeout=30, isolation_level=None)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        after = db_path.stat().st_size
        wal_after = wal_path.stat().st_size if wal_path.exists() else 0
        print(f"   ✓ 已压缩数据库：{before/1024/1024:.1f} MB → {after/1024/1024:.1f} MB，"
              f"WAL {wal_before/1024/1024:.1f} MB → {wal_after/1024/1024:.1f} MB"
              f"（已删除的请求日志已物理清除，不再可恢复）")
    except Exception as e:
        print(f"   ⚠️ 数据库压缩失败（若网关正在运行请先停止再清理）: {e}")


if __name__ == "__main__":
    confirm = input("⚠️  确定要清除所有用户数据吗？输入 yes 确认: ")
    if confirm.strip().lower() == "yes":
        clean()
    else:
        print("已取消。")
