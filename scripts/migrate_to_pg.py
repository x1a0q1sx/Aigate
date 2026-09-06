"""SQLite → PostgreSQL 一次性数据迁移工具（P2-11 配套）。

设计要点：
- 源库以只读模式打开（file:...?mode=ro），绝不写源库
- 目标表结构优先用 ORM create_all 创建（幂等），再反射真实表结构驱动类型转换
- 方言坑处理（docs/todo.md P2-11 备注）：
  * JSON 列：SQLite 原始行存字符串 → json.loads 反序列化后再写 PG
  * BOOLEAN 列：SQLite 存 0/1 整数 → 转 bool（asyncpg 不接受整型绑到 boolean）
  * TIMESTAMP 列：SQLite 存 ISO 字符串 → 转 datetime（asyncpg 不接受字符串绑到 timestamp）
- 表写入顺序按目标库反射出的外键依赖排序（sorted_tables）
- 逐批 ON CONFLICT DO NOTHING：可中断重跑，天然幂等
- 整批失败自动降级为逐行写入，坏行跳过并在结尾汇总
- 收尾重置各表 id 序列（pg_get_serial_sequence），保证切换后服务能继续插入
- VACUUM ANALYZE 更新统计信息（独立 AUTOCOMMIT 连接）

用法（建议在目标机器上执行，迁移期间避免写入源库；低峰执行更稳）：
  # 干跑：只统计源/目标行数
  python scripts/migrate_to_pg.py \
      --source data/aigate.db \
      --target postgresql+asyncpg://aigate:PASSWORD@127.0.0.1:5432/aigate --dry-run

  # 正式迁移（默认自动建缺失表；已有数据行按主键跳过）
  python scripts/migrate_to_pg.py --source data/aigate.db \
      --target postgresql+asyncpg://aigate:PASSWORD@127.0.0.1:5432/aigate

  # 清空目标表后全量迁移（注意：会清空目标库数据）
  python scripts/migrate_to_pg.py --source data/aigate.db \
      --target postgresql+asyncpg://... --truncate
"""
import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

# 允许在仓库根/任意位置运行：把 <repo>/ 加入 sys.path 以便 import server
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sqlalchemy import MetaData, insert, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

# 表写入顺序兜底：正常情况下按目标库反射的外键依赖排序（meta.sorted_tables），
# 这里只作为源表存在但目标无 FK 信息时的稳定序参考。
KNOWN_ORDER = [
    "providers", "api_keys", "models", "model_api_keys", "oauth_tokens",
    "combos", "intelligence_static", "routing_weights", "routing_pin",
    "rate_limits", "log_msg_blobs", "request_logs", "routing_decisions",
    "health_checks", "analytics_cumulative", "admin_audit_log",
]


def parse_args():
    p = argparse.ArgumentParser(description="AIGate SQLite → PostgreSQL 迁移工具")
    p.add_argument("--source", required=True, help="SQLite 源库文件路径")
    p.add_argument("--target", required=True, help="PostgreSQL 目标 URL（postgresql+asyncpg://user:pass@host:port/db）")
    p.add_argument("--dry-run", action="store_true", help="只统计行数，不写入")
    p.add_argument("--truncate", action="store_true", help="写入前 TRUNCATE 目标表（危险：清空目标库数据）")
    p.add_argument("--tables", default="", help="只迁移指定表（逗号分隔），默认全部")
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--no-create-schema", action="store_true", help="不在目标库自动建缺失表（默认会用 ORM create_all 补齐）")
    p.add_argument("--no-vacuum", action="store_true", help="迁移后不执行 VACUUM ANALYZE")
    return p.parse_args()


def _parse_dt(value: str):
    v = value.strip()
    if not v:
        return None
    # SQLite 常见形态：'2026-09-06 12:34:56' / '... 12:34:56.123456' / ISO 带 T/Z/时区
    v = v.replace(" ", "T", 1)
    if v.endswith("Z"):
        v = v[:-1]
    dt = None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(v, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError(f"无法解析时间戳: {value!r}")
    # asyncpg 拒绝 tz-aware 绑到 TIMESTAMP WITHOUT TIME ZONE：剥时区保留字面墙钟值
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def coerce_value(value, col_type):
    """按目标列类型转换 SQLite 原始值（docs/todo.md P2-11 备注的三类坑）。"""
    if value is None:
        return None
    py = None
    try:
        py = col_type.python_type
    except Exception:
        py = None

    if py is bool:
        if isinstance(value, bool):
            return value
        if value in (0, "0", "false", "False", "f", "n", "no"):
            return False
        if value in (1, "1", "true", "True", "t", "y", "yes"):
            return True
        raise ValueError(f"无法把 {value!r} 转为 boolean")
    if py is datetime:
        if isinstance(value, datetime):
            return value
        return _parse_dt(str(value))
    if py is date:
        if isinstance(value, datetime):
            return value
        return _parse_dt(str(value)).date() if isinstance(value, str) else value
    if py in (dict, list):
        if isinstance(value, (dict, list)):
            return value
        s = str(value).strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            # 不是合法 JSON（历史脏数据）：原样存字符串会双重编码，宁可丢格式也要可查
            return None
    return value


def open_source_ro(path: str) -> sqlite3.Connection:
    src_path = Path(path).resolve()
    if not src_path.is_file():
        raise SystemExit(f"[x] 源库不存在: {src_path}")
    conn = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def source_tables(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'alembic%'"
    ).fetchall()
    return {r["name"] for r in rows}


def order_tables(names):
    names = set(names)
    ordered = [t for t in KNOWN_ORDER if t in names]
    ordered += sorted(names - set(ordered))
    return ordered


async def main():
    args = parse_args()
    if not args.target.startswith("postgresql"):
        raise SystemExit("[x] 目标必须是 postgresql+asyncpg:// ...（防止源/目标写反）")
    t0 = time.monotonic()
    src = open_source_ro(args.source)
    src_all = source_tables(src)
    if args.tables:
        wanted = {t.strip() for t in args.tables.split(",") if t.strip()}
        missing = wanted - src_all
        if missing:
            raise SystemExit(f"[x] 源库中不存在这些表: {sorted(missing)}")
        table_names = order_tables(wanted)
    else:
        table_names = order_tables(src_all)

    engine = create_async_engine(args.target, echo=False, pool_pre_ping=True)

    # 1) 建缺失表（ORM create_all，幂等）
    if not args.no_create_schema and not args.dry_run:
        try:
            from server.db import Base  # noqa: 延迟导入，避免 dry-run 也触发配置加载
        except Exception as e:  # pragma: no cover
            print(f"[!] 无法导入 server.db（--no-create-schema 可跳过建表）：{e}")
        else:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            print("[+] 目标库缺失表已按 ORM 建齐")

    # 2) 反射目标结构（拿真实列类型驱动类型转换 + FK 依赖排序）
    meta = MetaData()
    async with engine.connect() as conn:
        await conn.run_sync(lambda sc: meta.reflect(bind=sc))

    target_by_name = {t: meta.tables[t] for t in meta.tables}
    plan = []
    for name in table_names:
        if name not in target_by_name:
            print(f"[!] 目标库没有表 {name} → 跳过；先不带 --no-create-schema 运行可自动建表")
            continue
        plan.append((name, target_by_name[name]))

    # 3) 行数预览 / truncate
    print("\n== 迁移计划 ==")
    for name, tgt in plan:
        s_cnt = src.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        print(f"  {name:28s} 源 {s_cnt:>8} 行 → 目标 {tgt.fullname}")
    if args.dry_run:
        print("\n[dry-run] 未做任何写入。")
        return

    if args.truncate:
        async with engine.begin() as conn:
            for name, _tgt in plan:
                await conn.execute(text(f'TRUNCATE TABLE "{name}" CASCADE'))
        print("[+] 目标表已 TRUNCATE")

    report = {}
    bad_rows = 0
    for name, tgt in plan:
        cols = {c.name: c for c in tgt.columns}
        src_cols = [r[1] for r in src.execute(f'PRAGMA table_info("{name}")').fetchall()]
        use_cols = [c for c in src_cols if c in cols]
        dropped = [c for c in src_cols if c not in cols]
        if dropped:
            print(f"[!] {name}: 源多出的列 {dropped} 将被丢弃（目标无此列）")

        pk = [c.name for c in tgt.primary_key.columns]
        stmt = pg_insert(tgt).on_conflict_do_nothing(index_elements=pk) if pk \
            else pg_insert(tgt).on_conflict_do_nothing()

        # 源库孤儿预检：SQLite 不强制外键，历史遗留行可能引用已删除的父记录，
        # PG 会拒绝这类行 —— 按目标库父表实况提前分拣为「孤儿跳过」而非计入坏行
        guards = []
        for fk in tgt.foreign_keys:
            try:
                lcol, ptable, pcol = fk.parent.name, fk.column.table.name, fk.column.name
                if lcol not in cols:
                    continue
                async with engine.connect() as conn:
                    pset = set((await conn.execute(
                        text(f'SELECT "{pcol}" FROM "{ptable}" WHERE "{pcol}" IS NOT NULL')
                    )).scalars().all())
                guards.append((lcol, ptable, pset))
            except Exception:
                continue  # 拿不到父表实况就不拦截，交给 FK 报错兜底

        total = 0
        orphan_skipped = 0
        table_errors = []
        async with engine.connect() as conn:
            before = (await conn.execute(text(f'SELECT COUNT(*) FROM "{name}"'))).scalar()
        cur = src.execute(f'SELECT {", ".join(f"\"{c}\"" for c in use_cols)} FROM "{name}"')
        while True:
            batch = cur.fetchmany(args.batch_size)
            if not batch:
                break
            rows = []
            for r in batch:
                orphan = False
                for lcol, ptable, pset in guards:
                    v = r[lcol] if lcol in r.keys() else None
                    if v is not None and v not in pset:
                        orphan_skipped += 1
                        orphan = True
                        break
                if orphan:
                    continue
                d = {}
                for c in use_cols:
                    try:
                        d[c] = coerce_value(r[c], cols[c].type)
                    except Exception as cv_err:
                        bad_rows += 1
                        if len(table_errors) < 5:
                            pkv = {k: r[k] for k in pk} if pk and pk[0] in r.keys() else ""
                            table_errors.append(f"{pkv} 列 {c}: {cv_err}")
                        d = None
                        break
                if d is not None:
                    rows.append(d)
            total += len(batch)
            if rows:
                try:
                    async with engine.begin() as conn:
                        await conn.execute(stmt, rows)
                except Exception as e:
                    # 整批失败 → 逐行降级，坏行跳过并记录前几条错误详情
                    msg = str(e).split("\n")[0][:160]
                    for row in rows:
                        try:
                            async with engine.begin() as conn:
                                await conn.execute(stmt, [row])
                        except Exception as row_err:
                            bad_rows += 1
                            if len(table_errors) < 5:
                                key = {k: row[k] for k in pk} if pk else row
                                table_errors.append(f"{key}: {str(row_err).splitlines()[0][:200]}")
                    print(f"[!] {name}: 批量失败已降级逐行（{msg}）")
        async with engine.connect() as conn:
            after = (await conn.execute(text(f'SELECT COUNT(*) FROM "{name}"'))).scalar()
        inserted = max(after - before, 0)
        report[name] = (total, inserted, orphan_skipped)
        print(f"  [ok] {name:28s} 读 {total:>8} / 插入 {inserted:>8}"
              + (f" / 孤儿跳过 {orphan_skipped}" if orphan_skipped else ""))
        for te in table_errors:
            print(f"       [x] {te}")

    # 4) 重置序列（保证切换后服务可继续插入）
    async with engine.begin() as conn:
        for name, tgt in plan:
            pk_cols = list(tgt.primary_key.columns)
            if len(pk_cols) != 1 or pk_cols[0].type.python_type is not int:
                continue
            pk = pk_cols[0].name
            seq = (await conn.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": name, "c": pk}
            )).scalar()
            if not seq:
                continue
            mx = (await conn.execute(text(f'SELECT COALESCE(MAX("{pk}"), 0) FROM "{name}"'))).scalar()
            if mx and mx > 0:
                await conn.execute(text(f'SELECT setval(:s, :v, true)'), {"s": seq, "v": mx})
            else:
                await conn.execute(text(f'SELECT setval(:s, 1, false)'), {"s": seq})
    print("[+] 各表 id 序列已对齐 MAX(id)")

    # 5) VACUUM ANALYZE（独立 AUTOCOMMIT 连接）
    if not args.no_vacuum:
        vac = create_async_engine(args.target, isolation_level="AUTOCOMMIT")
        try:
            async with vac.connect() as conn:
                for name, _t in plan:
                    await conn.execute(text(f'VACUUM ANALYZE "{name}"'))
            print("[+] VACUUM ANALYZE 完成（统计信息已更新）")
        finally:
            await vac.dispose()

    await engine.dispose()
    src.close()
    secs = time.monotonic() - t0
    total_orphans = sum(o for _, _, o in report.values())
    print(f"\n== 完成 == 耗时 {secs:.1f}s；坏行 {bad_rows}；源库孤儿跳过 {total_orphans}")
    for name, (total, inserted, orph) in report.items():
        print(f"  {name:28s} 读 {total:>8} / 插入 {inserted:>8}"
              + (f" / 孤儿跳过 {orph}" if orph else ""))
    if total_orphans:
        print("[~] 孤儿行引用的是源库里已删除的父记录（SQLite 未强制外键的历史遗留），"
              "PG 正确拒绝；如需保留请先在源库清理引用")
    if bad_rows:
        print("[x] 存在坏行，请检查上方日志（常见原因：varchar 长度溢出 / 脏数据）")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
