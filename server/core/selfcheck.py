"""C4: 启动自检与版本信息。

- get_version_info(): 当前版本（git commit/branch）、Python、DB 类型、运行时长
  —— 供 /admin/api/version 与前端页脚展示。
- run_selfcheck(): 启动时关键资源盘点（服务商/模型/密钥/组合/网关密钥计数、
  写入队列与代理池状态），启动日志输出一行摘要，异常资源项置 ok=False。
"""
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import text

from ..db import AsyncSessionLocal, IS_SQLITE

_ROOT = Path(__file__).resolve().parent.parent.parent
_started_at = time.time()
_git_cache = None


def get_git_info() -> dict:
    global _git_cache
    if _git_cache is None:
        info = {"commit": "", "branch": ""}
        try:
            def _run(*args):
                return subprocess.run(
                    ["git", *args], cwd=str(_ROOT), capture_output=True,
                    text=True, timeout=5,
                ).stdout.strip()
            info["commit"] = _run("rev-parse", "--short", "HEAD")
            info["branch"] = _run("rev-parse", "--abbrev-ref", "HEAD")
        except Exception:
            pass  # 非 git 部署（如 zip 解压）时留空
        _git_cache = info
    return dict(_git_cache)


def get_version_info() -> dict:
    g = get_git_info()
    return {
        "app": "AIGate",
        "version": g["commit"] or "dev",
        "commit": g["commit"],
        "branch": g["branch"],
        "python": sys.version.split()[0],
        "uptime_seconds": int(time.time() - _started_at),
        "database": "sqlite" if IS_SQLITE else "postgresql",
    }


async def run_selfcheck() -> dict:
    """启动自检。返回 {ok, items:[{name, ok, detail}]}，任何一项失败整体 ok=False。"""
    items = []

    def _item(name, ok, detail=""):
        items.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    try:
        async with AsyncSessionLocal() as db:
            providers = (await db.execute(text("SELECT COUNT(*) FROM providers"))).scalar() or 0
            models = (await db.execute(text("SELECT COUNT(*) FROM models"))).scalar() or 0
            keys = (await db.execute(text("SELECT COUNT(*) FROM api_keys"))).scalar() or 0
            combos = (await db.execute(text("SELECT COUNT(*) FROM combos"))).scalar() or 0
            gateway_keys = 0
            try:  # D1 之前该表不存在，容忍缺失
                gateway_keys = (await db.execute(
                    text("SELECT COUNT(*) FROM gateway_keys WHERE enabled=1")
                )).scalar() or 0
            except Exception:
                pass
            _item("database", True, "sqlite" if IS_SQLITE else "postgresql")
            _item("providers", True, f"{providers}")
            _item("models", True, f"{models}")
            _item("upstream_keys", True, f"{keys}")
            _item("combos", True, f"{combos}")
            _item("gateway_keys", True, f"{gateway_keys} enabled")
    except Exception as e:
        _item("database", False, str(e)[:160])
    try:
        from .log_queue import is_running, stats as lq_stats
        _item("log_queue", is_running(),
              f"queued={lq_stats.get('queued', 0)} dropped={lq_stats.get('dropped', 0)}")
    except Exception as e:
        _item("log_queue", False, str(e)[:160])
    try:
        from ..config import get_config
        pp = get_config().proxy_pool
        _item("proxy_pool", True, f"enabled={pp.enabled} n={len(pp.proxies)}")
    except Exception as e:
        _item("proxy_pool", False, str(e)[:160])
    return {"ok": all(i["ok"] for i in items), "items": items}
