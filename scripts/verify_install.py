"""Validate imports, schema migration, and SQLite integrity on an isolated DB."""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


REQUIRED_TABLES = {
    "providers",
    "api_keys",
    "models",
    "request_logs",
    "routing_decisions",
}


async def migrate(database: Path) -> None:
    os.environ["AIGATE_DATABASE_PATH"] = str(database)
    from server.db import engine, init_db

    await init_db()
    await engine.dispose()


def verify_database(database: Path) -> None:
    with sqlite3.connect(str(database)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise RuntimeError(f"Missing tables after migration: {', '.join(missing)}")
        decision_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(routing_decisions)")
        }
        required_columns = {"conversation_id", "candidates", "attempts", "fallback_count"}
        if not required_columns.issubset(decision_columns):
            raise RuntimeError("routing_decisions schema is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()

    database = args.database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    if args.config:
        config_path = args.config.resolve()
        if not config_path.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("auth:\n  enabled: false\n", encoding="utf-8")
        os.environ["AIGATE_CONFIG_PATH"] = str(config_path)

    asyncio.run(migrate(database))
    verify_database(database)

    from server.main import app
    if app.title != "AIGate":
        raise RuntimeError("FastAPI application import validation failed")

    print(f"Install verification passed: {database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
