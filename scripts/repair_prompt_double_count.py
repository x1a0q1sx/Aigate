#!/usr/bin/env python
"""F29 数据修复：还原被重复叠加的 prompt_tokens（缓存读被加了两遍）。

事故（2026-09-22 起，详见 server/core/usage_normalize.py 顶部注释 / docs/findings.md §F29）：
  上游 CodeBuddy 的 usage 同时携带
    · prompt_tokens                      （OpenAI 口径，**已含**缓存）
    · prompt_tokens_details.cached_tokens（命中数）
    · cache_read_input_tokens: 0 / cache_creation_input_tokens: 0（恒为 0 的占位键）
  旧归一化器按「键是否存在」判方言 → 误入 Anthropic 分支（其 input_tokens 不含
  缓存）→ 把缓存读又加了一遍：stored_prompt = raw_prompt + cache_read。

本脚本只修**逐行可证明**的行，判据三条同时成立：
  1) 能从原始 usage 取到 raw_prompt —— 三处来源按优先级合并：
     live 响应体 blob → 归档 jsonl.gz → 备份库
  2) 原始 usage 带 Anthropic 占位键特征（cache_read_input_tokens /
     cache_creation_input_tokens / cache_creation）
  3) stored_prompt == raw_prompt + cache_read（精确相等）
  → 修复为 raw_prompt（上游自己的计数）。修完 stored == raw，
    再跑一次不会二次扣减（幂等；UPDATE 带 prompt_tokens 原值守卫）。
  不满足证明的行一律不动，按原因计入 skipped 明细；差值对不上的反例单独列出。

用法（服务目录内执行）：
  PYTHONPATH=. venv/bin/python scripts/repair_prompt_double_count.py           # 干跑
  PYTHONPATH=. venv/bin/python scripts/repair_prompt_double_count.py --apply   # 落库
  可选：--archive-dir / --backup-dir 覆盖默认路径；--no-backups / --no-archives 跳过来源。
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Dict, Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

ANTH_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens", "cache_creation")


def usage_of(obj) -> dict:
    """从响应体（dict / list / {"__raw__": str} 包裹）里取最后一个 usage dict。"""
    if isinstance(obj, dict):
        if isinstance(obj.get("usage"), dict):
            return obj["usage"]
        inner = obj.get("__raw__")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except (ValueError, TypeError):
                return {}
        if isinstance(inner, list):
            u = {}
            for c in inner:
                if isinstance(c, dict) and isinstance(c.get("usage"), dict):
                    u = c["usage"]
            return u
        return {}
    if isinstance(obj, list):
        u = {}
        for c in obj:
            if isinstance(c, dict) and isinstance(c.get("usage"), dict):
                u = c["usage"]
        return u
    return {}


def blob_payload_to_obj(payload: bytes):
    try:
        return json.loads(gzip.decompress(payload).decode("utf-8", "replace"))
    except Exception:
        return None


def decide(row_pt: int, row_cr: int, raw: dict):
    """判定单行：返回 (action, repaired_pt, reason)。action ∈ {repair, skip}。"""
    if not isinstance(raw, dict) or not raw:
        return "skip", None, "no-raw-usage"
    rpt = raw.get("prompt_tokens")
    if not isinstance(rpt, (int, float)) or isinstance(rpt, bool) or rpt <= 0:
        return "skip", None, "raw-has-no-prompt_tokens"
    if not any(k in raw for k in ANTH_KEYS):
        return "skip", None, "no-anthropic-decoy-signature"
    if not row_cr:
        return "skip", None, "cache_read=0"
    if row_pt == int(rpt) + int(row_cr):
        return "repair", int(rpt), "proven-doubled"
    if row_pt == int(rpt):
        return "skip", None, "already-correct"
    return "skip", None, "unexpected-delta:%d" % (row_pt - int(rpt))


# ── 原始 usage 三处来源 ────────────────────────────────────────────

async def raw_from_live(db, ids: Iterable[int]) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    id_list = list(ids)
    for i in range(0, len(id_list), 800):
        chunk = id_list[i:i + 800]
        rows = (await db.execute(text(
            "SELECT r.id, b.payload FROM request_logs r "
            "JOIN log_msg_blobs b ON b.hash = r.response_body_hash "
            "WHERE r.id IN (%s)" % ",".join(str(x) for x in chunk)
        ))).all()
        for rid, payload in rows:
            obj = blob_payload_to_obj(payload)
            u = usage_of(obj)
            if u:
                out[int(rid)] = u
    return out


def raw_from_archives(archive_dir: str, ids: Iterable[int]) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    want = set(int(x) for x in ids)
    if not archive_dir or not os.path.isdir(archive_dir):
        return out
    for name in sorted(os.listdir(archive_dir)):
        if not name.endswith(".jsonl.gz"):
            continue
        fp = os.path.join(archive_dir, name)
        try:
            with gzip.open(fp, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or '"id"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if "_meta" in rec:
                        continue
                    rid = rec.get("id")
                    if rid is None or int(rid) not in want or int(rid) in out:
                        continue
                    u = usage_of(rec.get("response_body"))
                    if u:
                        out[int(rid)] = u
        except Exception as e:  # 归档损坏不阻断整体修复
            print("  [warn] 归档 %s 读取失败：%s" % (name, e))
    return out


def raw_from_backups(backup_dir: str, ids: Iterable[int]) -> Dict[int, dict]:
    """备份库兜底：归档已删除的旧行，其响应体仍留在当日备份里。"""
    out: Dict[int, dict] = {}
    want = set(int(x) for x in ids)
    if not backup_dir or not os.path.isdir(backup_dir):
        return out
    for name in sorted(os.listdir(backup_dir)):
        if not name.endswith(".db"):
            continue
        fp = os.path.join(backup_dir, name)
        try:
            con = sqlite3.connect("file:%s?mode=ro" % fp, uri=True)
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='log_msg_blobs'")
            if not cur.fetchone():
                con.close()
                continue
            rest = [x for x in want if x not in out]
            for i in range(0, len(rest), 800):
                chunk = rest[i:i + 800]
                cur.execute(
                    "SELECT r.id, b.payload FROM request_logs r "
                    "JOIN log_msg_blobs b ON b.hash = r.response_body_hash "
                    "WHERE r.id IN (%s)" % ",".join(str(x) for x in chunk))
                for rid, payload in cur.fetchall():
                    obj = blob_payload_to_obj(payload)
                    u = usage_of(obj)
                    if u:
                        out[int(rid)] = u
            con.close()
        except Exception as e:
            print("  [warn] 备份 %s 读取失败：%s" % (name, e))
    return out


# ── 主流程 ────────────────────────────────────────────────────────

async def run(args):
    from server.db import AsyncSessionLocal
    from server.config import get_config

    archive_dir = args.archive_dir or str(get_config().log_archive.archive_dir)
    backup_dir = args.backup_dir or os.path.join("data", "backups")

    async with AsyncSessionLocal() as db:
        where = "cache_read_tokens > 0"
        params = {}
        if args.provider:
            where += " AND routed_provider LIKE :pv"
            params["pv"] = args.provider
        targets = (await db.execute(text(
            "SELECT id, routed_provider, prompt_tokens, cache_read_tokens, estimated_cost_usd "
            "FROM request_logs WHERE " + where + " ORDER BY id"), params)).all()
        print("目标行（cache_read_tokens>0）：%d" % len(targets))
        if not targets:
            return

        ids = [int(t[0]) for t in targets]
        raw: Dict[int, dict] = {}
        src_stat = {}
        if not args.no_live:
            got = await raw_from_live(db, ids)
            src_stat["live"] = len(got)
            raw.update(got)
        if not args.no_archives:
            got = raw_from_archives(archive_dir, [x for x in ids if x not in raw])
            src_stat["archives"] = len(got)
            raw.update(got)
        if not args.no_backups:
            got = raw_from_backups(backup_dir, [x for x in ids if x not in raw])
            src_stat["backups"] = len(got)
            raw.update(got)
        print("原始 usage 覆盖：%s（合计 %d / %d）" %
              ("、".join("%s=%d" % kv for kv in src_stat.items()), len(raw), len(ids)))

        repairs = []
        skipped = defaultdict(int)
        by_provider = defaultdict(lambda: [0, 0])  # provider -> [repair, total]
        cost_rows = []
        for rid, pv, pt, cr, cost in targets:
            action, new_pt, reason = decide(int(pt or 0), int(cr or 0), raw.get(int(rid), {}))
            by_provider[pv or "-"][1] += 1
            if action == "repair":
                repairs.append((int(rid), int(pt), int(new_pt)))
                by_provider[pv or "-"][0] += 1
                if cost:
                    cost_rows.append((int(rid), pv, cost))
            else:
                skipped[reason] += 1

        print("\n判定结果：可修 %d / 目标 %d" % (len(repairs), len(targets)))
        print("  按服务商：")
        for pv, (rep, tot) in sorted(by_provider.items(), key=lambda x: -x[1][1]):
            print("    %-30s 可修 %5d / %5d" % (pv[:30], rep, tot))
        print("  跳过明细：")
        for reason, n in sorted(skipped.items(), key=lambda x: -x[1]):
            print("    %-34s %d" % (reason, n))
        if cost_rows:
            print("  ⚠️ 可修行中 estimated_cost_usd 非 0 的：%d 行（需人工确认是否重算成本）" % len(cost_rows))
            for rid, pv, c in cost_rows[:10]:
                print("    id=%s %s cost=%s" % (rid, pv, c))

        before = sum(r[1] for r in repairs)
        after = sum(r[2] for r in repairs)
        print("\nprompt_tokens 合计：%d → %d（减少 %d）" % (before, after, before - after))

        if not args.apply:
            print("\n[干跑] 未写库。加 --apply 落库。")
            return

        # 落库前先写可回滚凭据（id, 旧值, 新值）——修复必须可逆
        stamp = __import__("datetime").datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        rollback_path = args.rollback_log or ("data/repair_prompt_double_count-%s.json" % stamp)
        with open(rollback_path, "w", encoding="utf-8") as f:
            json.dump({"applied_at": stamp, "rows": [
                {"id": rid, "old_prompt_tokens": old, "new_prompt_tokens": new}
                for rid, old, new in repairs]}, f)
        print("回滚凭据已写入：%s（%d 行）" % (rollback_path, len(repairs)))

        done = 0
        for i in range(0, len(repairs), 500):
            chunk = repairs[i:i + 500]
            await db.execute(
                text("UPDATE request_logs SET prompt_tokens = :new "
                     "WHERE id = :id AND prompt_tokens = :old"),
                [{"id": rid, "old": old, "new": new} for rid, old, new in chunk],
            )
            done += len(chunk)
        await db.commit()
        print("已落库 %d 行。" % done)

        # 复核：逐行重读，确认 stored == raw 且无二次扣减
        bad = 0
        for rid, old, new in repairs:
            row = (await db.execute(text(
                "SELECT prompt_tokens, cache_read_tokens FROM request_logs WHERE id = :i"),
                {"i": rid})).one()
            raw_pt = int(raw[rid].get("prompt_tokens"))
            if int(row[0]) != raw_pt:
                bad += 1
                if bad <= 5:
                    print("  [复核失败] id=%s stored=%s raw=%s" % (rid, row[0], raw_pt))
        print("复核：%d 行与上游原值一致，异常 %d 行。" % (len(repairs) - bad, bad))

        agg = (await db.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(cache_read_tokens),0) "
            "FROM request_logs WHERE " + where), params)).one()
        n, pt, cr = int(agg[0]), int(agg[1]), int(agg[2])
        print("修复后该范围：%d 行, prompt=%d, cache_read=%d → 命中率=%.1f%%" %
              (n, pt, cr, (cr / pt * 100) if pt else 0.0))


def main():
    ap = argparse.ArgumentParser(description="F29 prompt_tokens 重复叠加修复（默认干跑）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只读干跑）")
    ap.add_argument("--provider", default="", help="限定 routed_provider LIKE 模式，默认全部")
    ap.add_argument("--archive-dir", default="", help="归档目录，默认取配置 log_archive.archive_dir")
    ap.add_argument("--backup-dir", default="", help="备份库目录，默认 data/backups")
    ap.add_argument("--no-live", action="store_true", help="跳过 live 响应体来源")
    ap.add_argument("--no-archives", action="store_true", help="跳过归档来源")
    ap.add_argument("--no-backups", action="store_true", help="跳过备份库来源")
    ap.add_argument("--rollback-log", default="", help="回滚凭据输出路径（默认 data/repair_prompt_double_count-<时间>.json）")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
