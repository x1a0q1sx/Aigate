"""C3: 数据库定时备份 + 保留策略。

- SQLite: 官方在线备份 API（sqlite3.Connection.backup），不停服务不锁库，
  WAL 也能得到一致快照。
- PostgreSQL: 调 pg_dump 自定义格式（-Fc，恢复用 pg_restore）。
- 保留策略: 超过 config.backup.keep 份时删除最旧。
- 产物命名: aigate-YYYYmmdd-HHMMSS.db / .sql（同目录混存按时间统一排序淘汰）。

注意: pg_dump 的连接串含密码，作为命令行参数在本机进程列表短暂可见；
单机自托管场景可接受，文档已注明。
"""
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

from ..config import get_config
from ..db import IS_SQLITE, engine


def _backup_dir() -> Path:
    d = Path(get_config().backup.dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _glob_backups() -> list:
    return sorted(_backup_dir().glob("aigate-*.db") + tuple(_backup_dir().glob("aigate-*.sql")),
                  key=lambda f: f.name)


def prune_backups(keep: int) -> list:
    files = _glob_backups()
    removed = []
    if keep > 0 and len(files) > keep:
        for f in files[: len(files) - keep]:
            try:
                f.unlink()
                removed.append(f.name)
            except Exception:
                pass
    return removed


async def run_backup(reason: str = "scheduled") -> dict:
    """执行一次备份。返回 {ok, file, size, reason, pruned} 或 {ok:False, error}。"""
    cfg = get_config().backup
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _backup_dir() / (f"aigate-{ts}.db" if IS_SQLITE else f"aigate-{ts}.sql")
    try:
        if IS_SQLITE:
            src = Path(get_config().database.path)
            if not src.exists():
                return {"ok": False, "error": f"源库不存在: {src}"}

            def _do_sqlite():
                s = sqlite3.connect(str(src))
                d = sqlite3.connect(str(dest))
                try:
                    with d:
                        s.backup(d)
                finally:
                    d.close()
                    s.close()
            await asyncio.to_thread(_do_sqlite)
        else:
            url = str(engine.url).replace("postgresql+asyncpg://", "postgresql://")
            proc = await asyncio.create_subprocess_exec(
                "pg_dump", "--no-owner", "-Fc", "-f", str(dest), url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                return {"ok": False, "error": f"pg_dump 失败: {err.decode(errors='replace')[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    size = dest.stat().st_size
    pruned = prune_backups(cfg.keep)
    return {"ok": True, "file": dest.name, "size": size, "reason": reason, "pruned": pruned}


def list_backups() -> list:
    items = []
    for f in reversed(_glob_backups()):
        st = f.stat()
        items.append({
            "name": f.name,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    return items
