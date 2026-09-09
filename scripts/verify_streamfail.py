# -*- coding: utf-8 -*-
"""触发 glm-5.3-free 真实失败并观察日志收尾（临时脚本）"""
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
    print("base_max_id =", base_max, flush=True)

    result = {}

    def do_chat():
        req = ur.Request(
            "http://127.0.0.1:8000/v1/chat/completions",
            data=json.dumps({"model": "tokenrouter官方/z-ai/glm-5.3-free", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}],
                             "max_tokens": 30}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        t0 = time.time()
        try:
            with ur.urlopen(req, timeout=420) as r:
                body = r.read().decode("utf-8", "replace")
                result["kind"] = "http200 tail=" + body[-100:].replace("\n", " ")
        except Exception as e:
            result["kind"] = "http%s %s" % (getattr(e, "code", 0), str(e)[:80])
        result["elapsed"] = round(time.time() - t0, 1)

    t = threading.Thread(target=do_chat)
    t.start()
    t.join()
    print("chat done:", result, flush=True)
    await asyncio.sleep(4)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT id, status, error_type, latency_ms, substr(error_msg,1,150) FROM request_logs "
            "WHERE id > :m AND is_health_check=0 ORDER BY id"), {"m": base_max})).all()
        for r in rows:
            print("ROW:", tuple(r), flush=True)


asyncio.run(main())
