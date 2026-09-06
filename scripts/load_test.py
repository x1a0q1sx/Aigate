"""AIGate 压测脚本（P2-12）。

用法（在服务器上执行）：
    python scripts/load_test.py --concurrency 10 --total 20 --model "combo:编程可用"
    python scripts/load_test.py --concurrency 50 --total 100 --model auto --timeout 180

观测指标：P50/P95 延迟、错误分类、日志落库差值、database is locked 次数。
注意：并发压力最终会传导到上游公益站——高并发请确认上游可承受。
"""
import argparse
import asyncio
import concurrent.futures
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "data" / "aigate.db"


def load_key():
    cfg = yaml.safe_load(open(BASE / "config.yaml", encoding="utf-8"))
    return cfg["security"]["aigate_api_key"]


def one(i, base, key, model, timeout):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "回复ok即可"}],
                       "max_tokens": 30}).encode()
    req = urllib.request.Request(f"{base}/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            ok = bool(d.get("choices"))
            return (i, time.time() - t0, None if ok else "empty_response")
    except urllib.error.HTTPError as e:
        return (i, time.time() - t0, f"http{e.code}")
    except Exception as e:
        return (i, time.time() - t0, f"{type(e).__name__}")


def recent_log_rows():
    if not DB_PATH.exists():
        return 0
    c = sqlite3.connect(DB_PATH)
    n = c.execute("SELECT COUNT(*) FROM request_logs WHERE created_at >= datetime('now','-10 minutes')").fetchone()[0]
    locked = c.execute("SELECT COUNT(*) FROM request_logs WHERE error_msg LIKE '%database is locked%'").fetchone()[0]
    c.close()
    return n, locked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--total", type=int, default=20)
    ap.add_argument("--model", default="combo:编程可用")
    ap.add_argument("--timeout", type=int, default=150)
    args = ap.parse_args()

    key = load_key()
    before, locked_before = recent_log_rows()
    print(f"load test: {args.total} requests / {args.concurrency} concurrency / model={args.model}")

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        results = list(ex.map(lambda i: one(i, args.base, key, args.model, args.timeout), range(args.total)))
    wall = time.time() - t0

    lat = sorted(r[1] for r in results)
    errs = [r for r in results if r[2]]
    p50 = lat[len(lat) // 2]
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    print(f"wall={wall:.1f}s  ok={len(results) - len(errs)}  errors={len(errs)}")
    print(f"latency: p50={p50:.1f}s  p95={p95:.1f}s  max={lat[-1]:.1f}s")
    if errs:
        from collections import Counter
        for k, v in Counter(e[2] for e in errs).most_common(5):
            print(f"  error {k}: {v}")

    time.sleep(3)  # 等日志队列 flush
    after, locked_after = recent_log_rows()
    print(f"log rows (10min): before={before} after={after} delta={after - before}")
    print(f"database is locked errors: {locked_after - locked_before} (delta)")
    print(f"log queue flush integrity: {'OK' if after - before >= args.total else 'CHECK (stream-cancelled requests may not log)'}")


if __name__ == "__main__":
    main()
