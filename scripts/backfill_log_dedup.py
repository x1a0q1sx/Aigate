"""
一次性回填脚本：把既有 request_logs 的 request_body / response_body（整包 TEXT）
迁移到消息级去重 blob 仓库（log_msg_blobs），行只留哈希引用。

用法（在 aigate-V1 目录下执行）：
  python scripts/backfill_log_dedup.py --dry-run      # 只统计，不写
  python scripts/backfill_log_dedup.py                # 执行迁移（写 blob + 写哈希）
  python scripts/backfill_log_dedup.py --verify      # 迁移后逐行校验 reassemble==原文（语义相等）
  python scripts/backfill_log_dedup.py --null-legacy --vacuum   # 迁移+校验通过后，清空旧 TEXT 并 VACUUM 释放空间

说明：
- 只处理 request_body 非空 且 request_msg_hashes 为空的“遗留行”，幂等可重跑。
- 校验用 json.loads 解析后比较（规范化后语义相等即视为正确，
  不要求字节一致，因为 pydantic 默认 dumps 与本项目规范化格式不同）。
"""
import argparse
import asyncio
import json
import os
import sys

# 把项目根目录加入 sys.path（无论从哪个 cwd 调用都能 import server.*）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text, update

from server.db import AsyncSessionLocal, engine
from server.models.request_log import RequestLog
from server.core.request_logger import (
    _store_request, _store_response, reassemble_request, reassemble_response,
)


async def _rows_to_migrate(db):
    stmt = (
        select(RequestLog)
        .where(RequestLog.request_body.isnot(None))
        .where(RequestLog.request_msg_hashes.is_(None))
        .order_by(RequestLog.id)
    )
    res = await db.execute(stmt)
    return res.scalars().all()


async def ensure_schema():
    """自包含地确保新表/列存在（幂等；与网关 init_db 的 ALTER 等价）。"""
    from server.db import engine, Base
    from server.models.request_log import LogMsgBlob  # noqa: F401 触发 import 以建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for sql in [
            "ALTER TABLE request_logs ADD COLUMN request_env_hash VARCHAR(64) DEFAULT NULL",
            "ALTER TABLE request_logs ADD COLUMN request_msg_hashes TEXT DEFAULT NULL",
            "ALTER TABLE request_logs ADD COLUMN response_body_hash VARCHAR(64) DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_log_msg_blobs_hash ON log_msg_blobs(hash)",
        ]:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass
    print("[schema] log_msg_blobs 与新列已就绪")


async def migrate(dry_run: bool):
    total = 0
    migrated = 0
    async with AsyncSessionLocal() as db:
        rows = await _rows_to_migrate(db)
        total = len(rows)
        print(f"[统计] 待迁移遗留行: {total}")
        if dry_run:
            return total
        batch = 0
        for r in rows:
            env_hash, msg_hashes = await _store_request(db, r.request_body)
            resp_hash = await _store_response(db, r.response_body)
            r.request_env_hash = env_hash
            r.request_msg_hashes = msg_hashes
            r.response_body_hash = resp_hash
            batch += 1
            if batch % 50 == 0:
                await db.commit()
                print(f"  ...已处理 {batch}/{total}")
        await db.commit()
        migrated = total
    print(f"[完成] 迁移 {migrated} 行，blob 已写入 log_msg_blobs")
    return migrated


async def verify():
    bad = 0
    checked = 0
    async with AsyncSessionLocal() as db:
        rows = await _rows_to_migrate(db)
        # 已迁移的行哈希非空，用另一种查法
        stmt = (
            select(RequestLog)
            .where(RequestLog.request_msg_hashes.isnot(None))
            .order_by(RequestLog.id)
        )
        res = await db.execute(stmt)
        rows = res.scalars().all()
        for r in rows:
            checked += 1
            ok = True
            # 请求体
            if r.request_body is not None:
                new = await reassemble_request(db, r)
                try:
                    if json.loads(new) != json.loads(r.request_body):
                        ok = False
                except Exception:
                    if new != r.request_body:
                        ok = False
            # 响应体
            if r.response_body is not None:
                new = await reassemble_response(db, r)
                try:
                    if json.loads(new) != json.loads(r.response_body):
                        ok = False
                except Exception:
                    if new != r.response_body:
                        ok = False
            if not ok:
                bad += 1
                if bad <= 10:
                    print(f"  ⚠️ 行 {r.id} reassemble 与原文字段不相等")
    print(f"[校验] 检查 {checked} 行，不一致 {bad} 行")
    return bad


async def null_legacy_and_vacuum():
    async with AsyncSessionLocal() as db:
        n = await db.execute(
            text("UPDATE request_logs SET request_body=NULL, response_body=NULL "
                  "WHERE request_msg_hashes IS NOT NULL")
        )
        await db.commit()
        print(f"[清理] 已清空旧 TEXT 列（影响行数={n.rowcount}）")
    print("[VACUUM] 开始回收空间（739MB 级，可能耗时数分钟）...")
    async with engine.begin() as conn:
        await conn.execute(text("VACUUM"))
    print("[VACUUM] 完成")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--null-legacy", action="store_true")
    ap.add_argument("--vacuum", action="store_true")
    args = ap.parse_args()

    await ensure_schema()

    if args.dry_run:
        await migrate(dry_run=True)
        return
    if args.verify:
        bad = await verify()
        sys.exit(1 if bad else 0)
    # 默认：迁移
    await migrate(dry_run=False)
    if args.null_legacy or args.vacuum:
        bad = await verify()
        if bad:
            print(f"[中止] 校验发现 {bad} 行不一致，不执行清空/回收，请排查")
            sys.exit(1)
        await null_legacy_and_vacuum()


if __name__ == "__main__":
    asyncio.run(main())
