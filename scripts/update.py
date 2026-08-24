"""Transactional AIGate updater with verified backup and automatic rollback.

The updater never validates migrations against the live database. It first runs
compile/tests/schema checks against an isolated copy, restarts through PM2, and
only commits the upgrade after an HTTP smoke check succeeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
IS_WIN = os.name == "nt"
KEEP_BACKUPS = 5
PM2_PROCESS = os.environ.get("AIGATE_PM2_NAME", "aigate")
STATUS_FILE = ROOT / "data" / "update_status.json"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


class UpdateFailure(RuntimeError):
    pass


@dataclass
class BackupBundle:
    root: Path
    database: Optional[Path]
    config: Optional[Path]
    frontend: Optional[Path]
    manifest: Path


def step(message: str) -> None:
    print(f"\n\033[36m> {message}\033[0m" if not IS_WIN else f"\n> {message}", flush=True)


def ok(message: str) -> None:
    print(f"  OK {message}", flush=True)


def warn(message: str) -> None:
    print(f"  WARN {message}", flush=True)


def die(message: str) -> None:
    raise UpdateFailure(message)


def _read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_status(state: str, **fields) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_status()
    payload.update({"state": state, "updated_at": datetime.now().isoformat(), **fields})
    temporary = STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATUS_FILE)


def run(cmd, cwd: Optional[Path] = None, *, capture: bool = True, check: bool = True, env: Optional[dict] = None):
    cwd = cwd or ROOT
    if isinstance(cmd, str):
        if IS_WIN:
            command = cmd
            shell = True
        else:
            command = shlex.split(cmd)
            shell = False
    else:
        command = [str(part) for part in cmd]
        shell = False
    result = subprocess.run(
        command,
        cwd=str(cwd),
        shell=shell,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if check and result.returncode != 0:
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        rendered = " ".join(command) if isinstance(command, list) else command
        die(f"Command failed: {rendered}\n{output}")
    return result


def git(args: str, *, check: bool = True):
    return run(["git", *shlex.split(args)], check=check)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(sha256(path).encode("ascii"))
    return digest.hexdigest()


def sqlite_integrity(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        die(f"SQLite integrity check failed for {path}: {result}")


def preflight(auto_stash: bool) -> str:
    step("Preflight")
    if not (ROOT / ".git").exists():
        die("Current directory is not a git repository")
    if not git("remote get-url origin", check=False).stdout.strip():
        die("Git remote 'origin' is not configured")

    branch = git("rev-parse --abbrev-ref HEAD").stdout.strip()
    dirty = git("status --porcelain --untracked-files=no").stdout.rstrip()
    if dirty:
        files = [line[3:] for line in dirty.splitlines()]
        if not auto_stash:
            die("Tracked local changes exist. Commit them or rerun with --stash:\n  " + "\n  ".join(files[:15]))
        git("stash push -m aigate-update-autostash")
        warn(f"Stashed {len(files)} tracked local changes")
    ok(f"branch={branch}")
    return branch


def _prune_backups(parent: Path) -> None:
    bundles = sorted(
        [path for path in parent.glob("update-*") if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old in bundles[KEEP_BACKUPS:]:
        shutil.rmtree(old)
        print(f"    removed old backup {old.name}", flush=True)


def verify_bundle(bundle: BackupBundle) -> None:
    try:
        manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        die(f"Backup manifest is unreadable: {exc}")
    if manifest.get("kind") != "aigate.update-backup":
        die("Backup manifest kind is invalid")

    file_entries = manifest.get("files") or {}
    for name, path in (("database", bundle.database), ("config", bundle.config)):
        if not path:
            continue
        expected = (file_entries.get(name) or {}).get("sha256")
        if not path.exists() or not expected or sha256(path) != expected:
            die(f"Backup checksum mismatch: {name}")
    if bundle.database:
        sqlite_integrity(bundle.database)
    if bundle.frontend:
        expected = (file_entries.get("frontend") or {}).get("tree_sha256")
        if not (bundle.frontend / "index.html").exists() or not expected or tree_sha256(bundle.frontend) != expected:
            die("Backup checksum mismatch: frontend")


def backup_state(before_commit: str) -> BackupBundle:
    step("Verified backup")
    parent = ROOT / "data" / "backups"
    parent.mkdir(parents=True, exist_ok=True)
    bundle_root = parent / f"update-{datetime.now():%Y%m%d-%H%M%S-%f}"
    bundle_root.mkdir(parents=True)

    source_db = ROOT / "data" / "aigate.db"
    database = bundle_root / "aigate.db" if source_db.exists() else None
    if database:
        source = None
        target = None
        try:
            source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
            target = sqlite3.connect(str(database))
            source.backup(target)
            target.commit()
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()
        try:
            sqlite_integrity(database)
        except Exception as exc:
            die(f"Database backup failed; update aborted: {exc}")

    source_config = ROOT / "config.yaml"
    config = bundle_root / "config.yaml" if source_config.exists() else None
    if config:
        shutil.copy2(source_config, config)

    source_frontend = ROOT / "client" / "dist"
    frontend = bundle_root / "client-dist" if source_frontend.exists() else None
    if frontend:
        shutil.copytree(source_frontend, frontend)

    files = {}
    for name, path in (("database", database), ("config", config)):
        if path:
            files[name] = {"file": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
    if frontend:
        files["frontend"] = {"file": frontend.name, "tree_sha256": tree_sha256(frontend)}

    manifest = bundle_root / "manifest.json"
    manifest.write_text(json.dumps({
        "kind": "aigate.update-backup",
        "created_at": datetime.now().isoformat(),
        "before_commit": before_commit,
        "files": files,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if os.name != "nt":
        for private_path in (database, config):
            if private_path:
                private_path.chmod(0o600)

    bundle = BackupBundle(bundle_root, database, config, frontend, manifest)
    verify_bundle(bundle)

    _prune_backups(parent)
    ok(f"backup={bundle_root.relative_to(ROOT)}")
    return bundle


def refresh_mutable_backup(bundle: BackupBundle) -> None:
    """Refresh DB/config immediately before restart to minimize rollback data loss."""
    source_db = ROOT / "data" / "aigate.db"
    if bundle.database and source_db.exists():
        temporary = bundle.database.with_suffix(".refresh")
        source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(str(temporary))
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()
        sqlite_integrity(temporary)
        temporary.replace(bundle.database)

    source_config = ROOT / "config.yaml"
    if bundle.config and source_config.exists():
        shutil.copy2(source_config, bundle.config)

    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    files = manifest.setdefault("files", {})
    if bundle.database:
        files["database"] = {
            "file": bundle.database.name,
            "bytes": bundle.database.stat().st_size,
            "sha256": sha256(bundle.database),
        }
    if bundle.config:
        files["config"] = {
            "file": bundle.config.name,
            "bytes": bundle.config.stat().st_size,
            "sha256": sha256(bundle.config),
        }
    manifest["refreshed_at"] = datetime.now().isoformat()
    bundle.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        for private_path in (bundle.database, bundle.config):
            if private_path:
                private_path.chmod(0o600)
    verify_bundle(bundle)


def pull(branch: str, dry_run: bool):
    step("Fetch update")
    git("fetch origin")
    before = git("rev-parse HEAD").stdout.strip()
    remote = f"origin/{branch}"
    if git(f"rev-parse --verify {remote}", check=False).returncode != 0:
        die(f"Remote branch not found: {remote}")

    ahead = int(git(f"rev-list --count {remote}..HEAD").stdout.strip() or 0)
    behind = int(git(f"rev-list --count HEAD..{remote}").stdout.strip() or 0)
    if behind == 0:
        ok("already up to date")
        return before, before, []

    changed = [line for line in git(f"diff --name-only HEAD..{remote}").stdout.splitlines() if line]
    print(f"  {behind} commits, {len(changed)} files", flush=True)
    if dry_run:
        for filename in changed[:30]:
            print(f"    {filename}", flush=True)
        return before, before, changed

    if ahead:
        result = git(f"pull --rebase origin {branch}", check=False)
        if result.returncode != 0:
            die("Git rebase failed:\n" + ((result.stdout or "") + (result.stderr or "")))
    else:
        git(f"merge --ff-only {remote}")
    after = git("rev-parse HEAD").stdout.strip()
    ok(f"code={before[:8]} -> {after[:8]}")
    return before, after, changed


def sync_python_deps(changed: list[str]) -> bool:
    if "requirements.txt" not in changed:
        return False
    step("Install Python dependencies")
    run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"], capture=False)
    ok("Python dependencies synchronized")
    return True


def build_frontend(changed: list[str], force: bool) -> bool:
    touched = [path for path in changed if path.startswith("client/") and not path.startswith("client/dist")]
    current = ROOT / "client" / "dist"
    if not force and not touched and (current / "index.html").exists():
        return False

    step("Build frontend")
    client = ROOT / "client"
    if not (client / "node_modules").exists():
        run("npm install", cwd=client, capture=False)
    elif any(path.endswith(("package.json", "package-lock.json")) for path in touched):
        run("npm install", cwd=client, capture=False)

    next_dist = client / "dist.update"
    shutil.rmtree(next_dist, ignore_errors=True)
    result = run("npx vite build --outDir dist.update --emptyOutDir=true", cwd=client, check=False)
    if result.returncode != 0 or not (next_dist / "index.html").exists():
        shutil.rmtree(next_dist, ignore_errors=True)
        die("Frontend build failed:\n" + ((result.stdout or "") + (result.stderr or "")))
    shutil.rmtree(current, ignore_errors=True)
    next_dist.replace(current)
    ok("frontend artifact replaced atomically")
    return True


def verify_release(bundle: BackupBundle) -> None:
    step("Release verification")
    set_status("running", phase="verify", backup=str(bundle.root.relative_to(ROOT)))
    run([sys.executable, "-m", "compileall", "-q", "server", "start.py", "scripts"])
    ok("Python compile")

    if (ROOT / "tests").exists():
        run([sys.executable, "-m", "pytest", "-q", "tests"], capture=False)
        ok("pytest")

    verify_dir = bundle.root / "verify"
    verify_dir.mkdir(exist_ok=True)
    verify_db = verify_dir / "aigate.db"
    if bundle.database:
        shutil.copy2(bundle.database, verify_db)
    verify_config = bundle.config or (ROOT / "config.yaml")
    command = [sys.executable, str(ROOT / "scripts" / "verify_install.py"), "--database", str(verify_db)]
    if verify_config and verify_config.exists():
        command.extend(["--config", str(verify_config)])
    run(command, capture=False)
    shutil.rmtree(verify_dir, ignore_errors=True)
    ok("isolated migration and app import")


def _pm2_name() -> Optional[str]:
    pm2 = os.environ.get("AIGATE_PM2") or shutil.which("pm2")
    if not pm2:
        return None
    for name in dict.fromkeys([PM2_PROCESS, "aigate", "Aigate"]):
        if run([pm2, "describe", name], check=False).returncode == 0:
            return name
    return None


def restart_service() -> str:
    step("Restart gateway")
    pm2 = os.environ.get("AIGATE_PM2") or shutil.which("pm2")
    name = _pm2_name()
    if not pm2 or not name:
        die("PM2 AIGate process not found; set AIGATE_PM2_NAME or use --no-restart")
    result = run([pm2, "restart", name], check=False)
    if result.returncode != 0:
        die("PM2 restart failed:\n" + ((result.stdout or "") + (result.stderr or "")))
    ok(f"PM2 process restarted: {name}")
    return name


def stop_service(name: Optional[str]) -> None:
    pm2 = os.environ.get("AIGATE_PM2") or shutil.which("pm2")
    if pm2 and name:
        run([pm2, "stop", name], check=False)


def restart_previous_service(name: Optional[str]) -> None:
    pm2 = os.environ.get("AIGATE_PM2") or shutil.which("pm2")
    if not pm2 or not name:
        die("Rollback restored files but cannot restart the previous service")
    restarted = run([pm2, "restart", name], check=False)
    if restarted.returncode != 0:
        die("Rollback restored files but PM2 failed to restart the previous service")
    smoke_check()


def _server_port() -> int:
    try:
        import yaml
        data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        return int((data.get("server") or {}).get("port") or 8000)
    except Exception:
        return 8000


def smoke_check(timeout_seconds: int = 40) -> None:
    step("Post-restart health check")
    url = f"http://127.0.0.1:{_server_port()}/"
    # Update commands may inherit HTTP(S)_PROXY for GitHub access. A local
    # health probe must never leave the machine or depend on that proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=3) as response:
                status = response.status
                payload = json.loads(response.read().decode("utf-8"))
            if status == 200 and payload.get("name") == "AIGate":
                ok(f"healthy={url}")
                return
            last_error = f"unexpected response: {payload}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(1)
    die(f"Gateway health check failed after {timeout_seconds}s: {last_error}")


def _restore_database(bundle: BackupBundle) -> None:
    if not bundle.database:
        return
    target = ROOT / "data" / "aigate.db"
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    source_connection = sqlite3.connect(f"file:{bundle.database}?mode=ro", uri=True, timeout=30)
    target_connection = sqlite3.connect(str(target), timeout=30)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    sqlite_integrity(target)


def rollback(bundle: BackupBundle, before_commit: str, service_name: Optional[str], service_touched: bool, reason: str) -> None:
    step("Automatic rollback")
    set_status("rolling_back", phase="rollback", rollback_reason=reason, backup=str(bundle.root.relative_to(ROOT)))
    verify_bundle(bundle)
    if service_touched:
        stop_service(service_name)

    if (ROOT / ".git" / "rebase-merge").exists() or (ROOT / ".git" / "rebase-apply").exists():
        git("rebase --abort", check=False)
    run(["git", "reset", "--hard", before_commit], check=True)

    if service_touched:
        if bundle.config:
            shutil.copy2(bundle.config, ROOT / "config.yaml")
        _restore_database(bundle)

    current_dist = ROOT / "client" / "dist"
    shutil.rmtree(current_dist, ignore_errors=True)
    if bundle.frontend:
        shutil.copytree(bundle.frontend, current_dist)

    if (ROOT / "requirements.txt").exists():
        run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"], check=False)

    if service_touched:
        restart_previous_service(service_name)
    ok(f"restored commit={before_commit[:8]}")


def execute_update(args) -> int:
    branch = preflight(args.stash)
    if args.check:
        pull(branch, dry_run=True)
        return 0

    before = git("rev-parse HEAD").stdout.strip()
    bundle = backup_state(before)
    service_name = _pm2_name()
    service_touched = False
    set_status("running", phase="pull", backup=str(bundle.root.relative_to(ROOT)), before_commit=before)

    try:
        before, after, changed = pull(branch, dry_run=False)
        if before == after and not args.rebuild:
            set_status("finished", phase="complete", before_commit=before, after_commit=after, backup=str(bundle.root.relative_to(ROOT)), message="Already up to date")
            return 0

        set_status("running", phase="build", after_commit=after)
        dependencies = sync_python_deps(changed)
        frontend = build_frontend(changed, args.rebuild)
        backend = any(path.startswith(("server/", "scripts/", "start.py")) for path in changed)
        verify_release(bundle)

        restarted = False
        if not args.no_restart and (backend or dependencies or frontend):
            refresh_mutable_backup(bundle)
            service_touched = bool(service_name)
            service_name = restart_service()
            service_touched = True
            set_status("running", phase="health_check", service=service_name)
            smoke_check()
            restarted = True

        set_status(
            "finished",
            phase="complete",
            before_commit=before,
            after_commit=after,
            changed_files=len(changed),
            backup=str(bundle.root.relative_to(ROOT)),
            service_restarted=restarted,
            rollback_performed=False,
        )
        print("\nUpdate complete", flush=True)
        print(f"  code       {before[:8]} -> {after[:8]}", flush=True)
        print(f"  backup     {bundle.root.relative_to(ROOT)}", flush=True)
        print(f"  restarted  {restarted}", flush=True)
        return 0
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        warn(f"update failed: {failure}")
        try:
            rollback(bundle, before, service_name, service_touched, failure)
            set_status(
                "rolled_back",
                phase="complete",
                before_commit=before,
                backup=str(bundle.root.relative_to(ROOT)),
                rollback_reason=failure,
                rollback_performed=True,
            )
            warn("Update failed and the previous version was restored")
        except Exception as rollback_error:
            set_status(
                "rollback_failed",
                phase="rollback",
                before_commit=before,
                backup=str(bundle.root.relative_to(ROOT)),
                rollback_reason=failure,
                rollback_error=f"{type(rollback_error).__name__}: {rollback_error}",
                rollback_performed=True,
            )
            print(f"Rollback failed: {rollback_error}", file=sys.stderr, flush=True)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Transactional AIGate updater")
    parser.add_argument("--check", action="store_true", help="preview remote changes")
    parser.add_argument("--stash", action="store_true", help="stash tracked local changes")
    parser.add_argument("--no-restart", action="store_true", help="verify update without restarting")
    parser.add_argument("--rebuild", action="store_true", help="force frontend rebuild")
    args = parser.parse_args()

    print("=" * 62)
    print("  AIGate transactional update")
    print("=" * 62)
    try:
        return execute_update(args)
    except KeyboardInterrupt:
        print("\nCancelled", flush=True)
        return 130
    except Exception as exc:
        set_status("error", phase="preflight", error=f"{type(exc).__name__}: {exc}")
        print(f"\nUpdate aborted: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
