import json
import sqlite3
import subprocess
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
