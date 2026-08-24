"""
一键更新 API
- /admin/api/update/check   检查更新（git fetch + 对比本地/远端 HEAD，统计落后提交与变更文件）
- /admin/api/update/apply   执行更新（后台运行 scripts/update.py --stash，输出重定向到日志文件）
- /admin/api/update/status  查询更新任务状态 / 最近一次更新日志

数据安全：
  更新脚本 scripts/update.py 内部会先做 SQLite 在线备份，且全程不触碰 config.yaml
  （加密密钥/代理/登录密码）与 data/archives 归档目录。组合顺序、服务商、模型、
  密钥等全部保存在 data/aigate.db 中 —— 更新前自动备份、更新中不会被覆盖。

网络：
  用户机器的 git 全局代理可能已失效（如旧 Clash 端口），本模块会探测本机可用代理
  端口（含 config.yaml 中 proxy_pool 的端口），并在 git 命令中临时注入可用代理，
  不改动任何全局 git 配置。
"""
import os
import sys
import socket
import json
import subprocess
import asyncio
from pathlib import Path
from typing import Optional
from fastapi import APIRouter

router = APIRouter(prefix="/admin/api/update", tags=["update"])

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_FILE = ROOT / "data" / "update.log"
_STATUS_FILE = ROOT / "data" / "update_status.json"

# 正在运行的后台更新进程（单实例）
_running: Optional[subprocess.Popen] = None
# 探测到的可用代理（进程级缓存）
_PROXY_CACHE: Optional[str] = None


def _probe_proxy() -> Optional[str]:
    """探测本机可用 HTTP 代理端口。优先 config 里 proxy_pool 的端口，其次常见 Clash 端口。"""
    global _PROXY_CACHE
    if _PROXY_CACHE:
        return _PROXY_CACHE
    candidates = []
    try:
        from server.config import get_config
        cfg = get_config()
        pool = getattr(cfg, "proxy_pool", None)
        if pool and getattr(pool, "enabled", False):
            for p in getattr(pool, "proxies", []) or []:
                url = str(p)
                if url.startswith("socks5://") or url.startswith("http://"):
                    hostport = url.split("//", 1)[-1].split("/")[0]
                    if ":" in hostport:
                        candidates.append(hostport)
    except Exception:
        pass
    candidates += ["127.0.0.1:7897", "127.0.0.1:10808", "127.0.0.1:7890", "127.0.0.1:10809", "127.0.0.1:1080"]
    seen = set()
    for hp in candidates:
        if hp in seen:
            continue
        seen.add(hp)
        try:
            host, port = hp.split(":", 1)
            with socket.create_connection((host, int(port)), timeout=1.0):
                _PROXY_CACHE = f"http://{hp}"
                return _PROXY_CACHE
        except Exception:
            continue
    return None


def _git(args: str, capture=True) -> subprocess.CompletedProcess:
    """执行 git 命令。探测到可用代理时临时注入 -c 参数（不改全局配置）。"""
    proxy = _probe_proxy()
    cmd = "git"
    if proxy:
        cmd += f' -c http.proxy={proxy} -c https.proxy={proxy}'
    cmd += f" {args}"
    return subprocess.run(
        cmd, cwd=str(ROOT), shell=True, capture_output=capture,
        text=True, encoding="utf-8", errors="replace",
    )


def _read_log(tail: int = 300) -> str:
    if not LOG_FILE.exists():
        return ""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])
    except Exception:
        return ""


def _read_status() -> dict:
    if not _STATUS_FILE.exists():
        return {}
    try:
        return json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_status(data: dict):
    try:
        _STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (TypeError, ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def _current_commit() -> dict:
    """读取本地 git 当前提交信息（无仓库时返回空）"""
    try:
        r = _git("rev-parse --short HEAD")
        sha = r.stdout.strip() if r.returncode == 0 else ""
        r2 = _git("log -1 --format=%s%n%ci")
        parts = r2.stdout.strip().splitlines() if r2.returncode == 0 else ["", ""]
        return {"sha": sha, "message": parts[0] if parts else "", "date": parts[1] if len(parts) > 1 else ""}
    except Exception:
        return {"sha": "", "message": "", "date": ""}


@router.get("/check")
async def check_update():
    """检查更新：git fetch 后对比本地/远端 HEAD，返回是否可更新及更新内容。"""
    current = _current_commit()
    result = {
        "current": current,
        "update_available": False,
        "message": "",
        "proxy": _probe_proxy(),  # 前端可据此提示网络状态
    }
    if not current.get("sha"):
        result["message"] = "当前目录不是 git 仓库，无法检查更新"
        return result
    try:
        branch = _git("rev-parse --abbrev-ref HEAD")
        branch = branch.stdout.strip() if branch.returncode == 0 and branch.stdout.strip() else "main"
    except Exception:
        branch = "main"
    # fetch 远端（仅更新远端引用，不改变工作区）
    r = _git(f"fetch origin {branch} --quiet")
    if r.returncode != 0:
        err = ((r.stderr or "") + (r.stdout or "")).strip()
        result["message"] = f"无法连接远端仓库（{err[:200]}）。请检查网络/代理后重试。"
        return result
    remote_ref = f"origin/{branch}"
    if _git(f"rev-parse --verify {remote_ref}", capture=True).returncode != 0:
        result["message"] = f"远端不存在分支 {branch}"
        return result
    remote_sha = _git(f"rev-parse --short {remote_ref}", capture=True).stdout.strip()
    if remote_sha == current["sha"]:
        result["message"] = "当前已是最新版本"
        return result
    try:
        r1 = _git(f"rev-list --count HEAD..{remote_ref}")
        behind = int(r1.stdout.strip()) if r1.returncode == 0 and r1.stdout.strip().isdigit() else 0
        r2 = _git(f"log --oneline --no-decorate HEAD..{remote_ref}")
        commits = [ln for ln in r2.stdout.strip().splitlines() if ln.strip()][:20] if r2.returncode == 0 else []
        r3 = _git(f"diff --name-only HEAD..{remote_ref}")
        files = [ln for ln in r3.stdout.strip().splitlines() if ln.strip()][:60] if r3.returncode == 0 else []
        result.update({
            "update_available": True,
            "remote": {"sha": remote_sha, "branch": branch},
            "behind": behind,
            "commits": commits,
            "files": files,
            "impact": {
                "backend": any(f.startswith(("server/", "start.py")) for f in files),
                "frontend": any(f.startswith("client/") and not f.startswith("client/dist") for f in files),
                "deps": "requirements.txt" in files,
            },
        })
    except Exception as e:
        result["message"] = f"获取更新详情失败：{e}"
    return result


@router.post("/apply")
async def apply_update():
    """后台执行更新（scripts/update.py --stash）。执行期间请勿重复触发。"""
    global _running
    current_status = _read_status()
    status_active = current_status.get("state") in {"running", "rolling_back"}
    if (_running is not None and _running.poll() is None) or (status_active and _pid_alive(current_status.get("pid"))):
        return {"ok": False, "message": "已有更新任务正在执行，请稍后再试"}
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _write_status({"state": "running", "started_at": __import__("datetime").datetime.now().isoformat()})
    try:
        # 给子进程注入可用代理环境变量（update.py 内部 git 命令会继承）
        env = os.environ.copy()
        proxy = _probe_proxy()
        if proxy:
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            process_options = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            }
            _running = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / "update.py"), "--stash"],
                cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                env=env,
                **process_options,
            )
        _write_status({
            "state": "running",
            "phase": "preflight",
            "started_at": __import__("datetime").datetime.now().isoformat(),
            "pid": _running.pid,
        })
        return {"ok": True, "message": "事务更新已开始；验证或健康检查失败时将自动回滚。"}
    except Exception as e:
        _write_status({"state": "error", "error": str(e)})
        return {"ok": False, "message": f"启动更新失败：{e}"}


@router.get("/status")
async def update_status():
    """查询更新任务状态与最近日志。"""
    global _running
    status = _read_status()
    local_running = _running is not None and _running.poll() is None
    status_active = status.get("state") in {"running", "rolling_back"}
    running = local_running or (status_active and _pid_alive(status.get("pid")))
    if not running and status.get("state") in {"running", "rolling_back"}:
        # 进程已结束（成功或失败）
        if _running is not None and _running.poll() is not None:
            if status.get("state") == "rolling_back":
                state = "rollback_failed"
            else:
                state = "error" if _running.returncode != 0 else "finished"
            _write_status({**status, "state": state, "exit_code": _running.returncode})
            status = _read_status()
        else:
            state = "rollback_failed" if status.get("state") == "rolling_back" else "error"
            _write_status({**status, "state": state, "error": "进程异常退出"})
            status = _read_status()
    return {
        "running": running,
        "state": status.get("state", "idle"),
        "started_at": status.get("started_at"),
        "exit_code": status.get("exit_code"),
        "error": status.get("error"),
        "phase": status.get("phase"),
        "pid": status.get("pid"),
        "backup": status.get("backup"),
        "before_commit": status.get("before_commit"),
        "after_commit": status.get("after_commit"),
        "rollback_performed": status.get("rollback_performed", False),
        "rollback_reason": status.get("rollback_reason"),
        "rollback_error": status.get("rollback_error"),
        "service_restarted": status.get("service_restarted", False),
        "log_tail": _read_log(),
    }


def _backup_items() -> list:
    backup_root = ROOT / "data" / "backups"
    items = []
    for directory in sorted(backup_root.glob("update-*"), reverse=True):
        manifest_path = directory / "manifest.json"
        if not directory.is_dir() or not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = manifest.get("files") or {}
            database = files.get("database") or {}
            items.append({
                "name": directory.name,
                "created_at": manifest.get("created_at"),
                "refreshed_at": manifest.get("refreshed_at"),
                "commit": manifest.get("before_commit"),
                "database_bytes": database.get("bytes"),
                "database_sha256": database.get("sha256"),
                "has_config": "config" in files,
                "has_frontend": "frontend" in files,
            })
        except Exception:
            continue
    return items[:5]


@router.get("/backups")
async def list_update_backups():
    return {"items": _backup_items()}


@router.post("/backups")
async def create_update_backup():
    status = _read_status()
    status_active = status.get("state") in {"running", "rolling_back"}
    if (_running is not None and _running.poll() is None) or (status_active and _pid_alive(status.get("pid"))):
        return {"ok": False, "message": "更新进行中，暂不能创建额外恢复点"}
    try:
        from scripts.update import backup_state

        commit = _current_commit().get("sha") or "unknown"
        bundle = await asyncio.to_thread(backup_state, commit)
        return {
            "ok": True,
            "message": "恢复点已创建",
            "backup": str(bundle.root.relative_to(ROOT)),
            "items": _backup_items(),
        }
    except Exception as exc:
        return {"ok": False, "message": f"创建恢复点失败：{exc}"}
