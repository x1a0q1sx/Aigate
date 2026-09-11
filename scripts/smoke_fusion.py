# -*- coding: utf-8 -*-
"""fusion 真实冒烟：取最近成功的两个模型建 fusion 组合 → 请求 → 验证日志（临时）"""
import asyncio
import json
import urllib.request

async def main():
    from sqlalchemy import text
    from server.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        pairs = (await db.execute(text(
            "SELECT DISTINCT routed_provider, routed_model FROM request_logs "
            "WHERE status='success' AND is_health_check=0 AND routed_provider IS NOT NULL "
            "AND routed_model IS NOT NULL ORDER BY 1 LIMIT 5"))).all()
    cands = [(r[0], r[1]) for r in pairs if r[0]][:2]
    if len(cands) < 2:
        print("not enough successful models found, abort"); return
    print("candidates:", cands)

    async with AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM combos WHERE name='fusion-smoke'"))
        import datetime
        now = datetime.datetime.utcnow()
        await db.execute(text(
            "INSERT INTO combos (name, description, strategy, model_ids, priority, enabled, created_at, updated_at, fusion_config) "
            "VALUES ('fusion-smoke','smoke','fusion',:mid,0,1,:c,:u,:fc)"),
            {"mid": json.dumps([{"provider": p, "model_id": m} for p, m in cands], ensure_ascii=False),
             "c": now, "u": now,
             "fc": json.dumps({"judge": None, "max_targets": 6, "timeout_seconds": 40})})
        await db.commit()

    import yaml
    cfg = yaml.safe_load(open('config.yaml', encoding='utf-8'))
    key = cfg['security']['aigate_api_key']
    body = {"model": "combo:fusion-smoke",
            "messages": [{"role": "user", "content": "用一句话说明你是谁"}],
            "max_tokens": 60}
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            out = json.loads(r.read())
        ch = (out.get("choices") or [{}])[0]
        print("HTTP", r.status, "| fusion:", (out.get("aigate_fusion") or {}).get("fused"),
              "| sources:", (out.get("aigate_fusion") or {}).get("sources"))
        print("answer:", (ch.get("message") or {}).get("content", "")[:120])
    except Exception as e:
        print("request failed:", getattr(e, 'code', 0), (e.read() if hasattr(e, 'read') else str(e))[:300])

    await asyncio.sleep(3)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT status, routed_provider, routed_model, latency_ms, error_type, substr(error_msg,1,150) "
            "FROM request_logs WHERE requested_model='combo:fusion-smoke' ORDER BY id DESC LIMIT 1"))).all()
        for x in rows:
            print("log:", tuple(x))
        dec = (await db.execute(text(
            "SELECT attempts FROM routing_decisions WHERE requested_model='combo:fusion-smoke' ORDER BY id DESC LIMIT 1"))).all()
        if dec and dec[0][0]:
            print("decision attempts:", dec[0][0][:400])
        await db.execute(text("DELETE FROM combos WHERE name='fusion-smoke'"))
        await db.commit()
        print("smoke combo cleaned")

asyncio.run(main())
