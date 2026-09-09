# -*- coding: utf-8 -*-
"""pending 行实时可见性验证（临时脚本）"""
import asyncio
import json
import os
import sys
import threading
import time
import urllib.request as ur

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
os.chdir(_REPO)

import yaml

async def main():
    from sqlalchemy import text
    from server.db import AsyncSessionLocal
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    key = cfg["security"]["aigate_api_key"]

    async with AsyncSessionLocal() as db:
        base_max = (await db.execute(text("SELECT COALESCE(MAX(id),0) FROM request_logs"))).scalar()

    result = {}

    def do_chat():
        req = ur.Request(
            "http://127.0.0.1:8000/v1/chat/completions",
            data=json.dumps({"model": "deepseek-v4-flash",
                             "messages": [{"role": "user", "content": "请用一句话介绍你自己"}],
                             "max_tokens": 100}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        t0 = time.time()
        try:
            with ur.urlopen(req, timeout=120) as r:
                result["status"] = r.status
        except Exception as e:
            result["status"] = getattr(e, "code", 0)
        result["elapsed"] = round(time.time() - t0, 1)

    t = threading.Thread(target=do_chat)
    t.start()
    pending_row = None
    for _ in range(20):
        await asyncio.sleep(0.5)
        async with AsyncSessionLocal() as db:
            row = (await db.execute(text(
                "SELECT id, status, requested_model FROM request_logs "
                "WHERE id > :m AND status='pending' ORDER BY id DESC LIMIT 1"),
                {"m": base_max})).first()
        if row:
            pending_row = row
            break
    if pending_row:
        print("✓ 进行中可见: id=%s status=%s model=%s" % tuple(pending_row))
    else:
        print("✗ 未捕获到 pending 行（请求可能太快完成）")
    t.join()
    print("chat done: status=%s elapsed=%ss" % (result.get("status"), result.get("elapsed")))
    await asyncio.sleep(3)

    if pending_row:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(text(
                "SELECT id, status, latency_ms FROM request_logs WHERE conversation_id = "
                "(SELECT conversation_id FROM request_logs WHERE id = :i) ORDER BY id"),
                {"i": pending_row[0]})).all()
        print("同一会话行数:", len(rows), rows)
        assert len(rows) == 1, "应原位更新为单行"
        assert rows[0][1] in ("success", "error"), "应变为终态"
        assert rows[0][0] == pending_row[0], "id 应保持不变"
        print("✓ 原位更新验证通过：pending 行 id 不变，已变为", rows[0][1])

asyncio.run(main())
