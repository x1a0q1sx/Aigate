import json
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts import update


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _create_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _database_value(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        connection.close()


def _set_database_value(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE marker SET value = ?", (value,))
        connection.commit()
    finally:
        connection.close()


def test_backup_manifest_and_rollback_restore_all_state(tmp_path, monkeypatch):
    repo = tmp_path / "aigate"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")

    tracked = repo / "server.txt"
    tracked.write_text("old-code", encoding="utf-8")
    _git(repo, "add", "server.txt")
    _git(repo, "commit", "-m", "old")
    before = _git(repo, "rev-parse", "HEAD")

    database = repo / "data" / "aigate.db"
    _create_database(database, "old-data")
    (repo / "config.yaml").write_text("security:\n  encryption_key: old\n", encoding="utf-8")
    dist = repo / "client" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("old-ui", encoding="utf-8")

    monkeypatch.setattr(update, "ROOT", repo)
    monkeypatch.setattr(update, "STATUS_FILE", repo / "data" / "update_status.json")
    bundle = update.backup_state(before)

    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    assert manifest["kind"] == "aigate.update-backup"
    assert manifest["before_commit"] == before
    assert manifest["files"]["database"]["sha256"] == update.sha256(bundle.database)

    tracked.write_text("new-code", encoding="utf-8")
    _git(repo, "add", "server.txt")
    _git(repo, "commit", "-m", "new")
    _set_database_value(database, "new-data")
    (repo / "config.yaml").write_text("security:\n  encryption_key: new\n", encoding="utf-8")
    (dist / "index.html").write_text("new-ui", encoding="utf-8")

    monkeypatch.setattr(update, "stop_service", lambda name: None)
    monkeypatch.setattr(update, "restart_previous_service", lambda name: None)
    update.rollback(bundle, before, service_name="aigate", service_touched=True, reason="test failure")

    assert _git(repo, "rev-parse", "HEAD") == before
    assert tracked.read_text(encoding="utf-8") == "old-code"
    assert _database_value(database) == "old-data"
    assert "old" in (repo / "config.yaml").read_text(encoding="utf-8")
    assert (dist / "index.html").read_text(encoding="utf-8") == "old-ui"


def test_sqlite_integrity_rejects_corrupt_backup(tmp_path):
    corrupt = tmp_path / "bad.db"
    corrupt.write_bytes(b"not a sqlite database")
    try:
        update.sqlite_integrity(corrupt)
    except (update.UpdateFailure, sqlite3.DatabaseError):
        pass
    else:
        raise AssertionError("corrupt SQLite backup was accepted")


def test_finished_status_clears_stale_rollback_diagnostics(tmp_path, monkeypatch):
    status_file = tmp_path / "data" / "update_status.json"
    status_file.parent.mkdir(parents=True)
    status_file.write_text(
        json.dumps({
            "state": "rolled_back",
            "rollback_reason": "old failure",
            "rollback_error": "old failure",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(update, "STATUS_FILE", status_file)

    payload = update.set_status("finished", after_commit="new")

    assert payload["after_commit"] == "new"
    assert "rollback_reason" not in payload
    assert "rollback_error" not in payload
    assert "rollback_reason" not in json.loads(status_file.read_text(encoding="utf-8"))


def test_refresh_mutable_backup_captures_latest_pre_restart_state(tmp_path, monkeypatch):
    root = tmp_path / "aigate"
    database = root / "data" / "aigate.db"
    _create_database(database, "initial")
    (root / "config.yaml").write_text("version: initial\n", encoding="utf-8")

    monkeypatch.setattr(update, "ROOT", root)
    monkeypatch.setattr(update, "STATUS_FILE", root / "data" / "update_status.json")
    bundle = update.backup_state("abc123")

    _set_database_value(database, "latest-before-restart")
    (root / "config.yaml").write_text("version: latest-before-restart\n", encoding="utf-8")
    update.refresh_mutable_backup(bundle)

    _set_database_value(database, "broken-new-version")
    update._restore_database(bundle)
    assert _database_value(database) == "latest-before-restart"
    assert "latest-before-restart" in bundle.config.read_text(encoding="utf-8")
    update.verify_bundle(bundle)


def test_smoke_check_bypasses_inherited_http_proxy(tmp_path, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"name":"AIGate"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = tmp_path / "aigate"
        root.mkdir()
        (root / "config.yaml").write_text(
            f"server:\n  port: {server.server_address[1]}\n", encoding="utf-8"
        )
        monkeypatch.setattr(update, "ROOT", root)
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        update.smoke_check(timeout_seconds=2)
    finally:
        server.shutdown()
        thread.join(timeout=2)
