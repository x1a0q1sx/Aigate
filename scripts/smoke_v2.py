# -*- coding: utf-8 -*-
"""v2 冒烟：22 项路线新能力的端到端验证（跑完即删）"""
import asyncio
import json
import os
import sys
import urllib.request as ur

BASE = os.environ.get("AIGATE_BASE", "http://127.0.0.1:8000")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
os.chdir(_REPO)


def http_json(path, data=None, key=None, timeout=120):
    req = ur.Request(BASE + path)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with ur.urlopen(req, data=body, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except Exception as e:
        return getattr(e, "code", 0), {"error": str(e)[:200]}


async def main():
    ok = fail = 0
    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✓ {name} {detail}")
        else:
            fail += 1
            print(f"  ✗ {name} {detail}")

    # ── A2: /v1/models 组合 + 思考强度变体 ──
    st, models = http_json("/v1/models")
    ids = [m["id"] for m in models.get("data", [])]
    combo_ids = [i for i in ids if i.startswith("combo:")]
    check("A2 /v1/models", st == 200, f"total={len(ids)} combo={len(combo_ids)} {combo_ids[:2]}")
    st, eff = http_json("/v1/models?include_effort=true")
    variants = [m for m in eff.get("data", []) if m.get("aigate_effort_variant")]
    check("A2 effort variants", st == 200 and len(variants) > 0, f"n={len(variants)}")

    # ── A1: /v1beta/models ──
    st, gm = http_json("/v1beta/models")
    check("A1 /v1beta/models", st == 200 and len(gm.get("models", [])) > 0,
          f"n={len(gm.get('models', []))}")

    # 选一个最近成功请求过的真实模型做对话验证
    from sqlalchemy import text
    from server.db import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        row = (await db.execute(text(
            "SELECT requested_model, routed_provider, routed_model FROM request_logs "
            "WHERE status='success' AND is_health_check=0 AND routed_model IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"))).first()
    model_name = row[0] if row else ids[0]
    print(f"  · 用真实模型验证对话: {model_name} ({row[1] if row else '?'}/{row[2] if row else '?'})")

    import yaml
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    master_key = cfg["security"]["aigate_api_key"]

    # ── D1/D2: 网关密钥全链路 ──
    from server.api.admin_ext_router import (
        GatewayKeyCreate, create_gateway_key, delete_gateway_key, gateway_key_usage)
    from server.api.admin_ext_router import (
        AliasCreate, create_alias, delete_alias)
    from server.api.admin_ops_router import (
        diagnose, failure_analysis, live_summary, price_health)
    from server.core.backup_service import list_backups, run_backup

    async with AsyncSessionLocal() as db:
        created = await create_gateway_key(GatewayKeyCreate(
            name="smoke", daily_token_limit=100000, over_limit_action="reject"), db)
        gk_id, gk_plain = created["key"]["id"], created["plaintext"]
        print(f"  · 网关密钥已创建 id={gk_id} prefix={created['key']['key_prefix']}")

        # 用网关密钥真实对话
        st, resp = http_json("/v1/chat/completions",
                             {"model": model_name, "messages": [{"role": "user", "content": "reply with exactly: ok"}],
                              "max_tokens": 10}, key=gk_plain)
        check("D1 网关密钥真实对话", st == 200 and resp.get("choices"), f"status={st} content={str(resp.get('choices',[{}])[0].get('message',{}).get('content'))[:30]!r}")

        # 日志带 downstream_key_id（等待后台写队列 flush ~500ms 批量）
        await asyncio.sleep(3)
        drows = (await db.execute(text(
            "SELECT downstream_key_id FROM request_logs WHERE is_health_check=0 "
            "ORDER BY id DESC LIMIT 4"))).all()
        got = [r[0] for r in drows]
        check("D1 日志关联 downstream_key_id", gk_id in got, f"recent={got}")

        # 用量接口
        usage = await gateway_key_usage(gk_id, db)
        check("D2 用量聚合", usage["usage"]["requests"] >= 1,
              f"req={usage['usage']['requests']} tokens={usage['usage']['tokens']}")

        # 主密钥仍然可用
        st2, _r2 = http_json("/v1/chat/completions",
                             {"model": model_name, "messages": [{"role": "user", "content": "reply ok"}],
                              "max_tokens": 5}, key=master_key)
        check("D1 主密钥兼容", st2 == 200, f"status={st2}")

        # ── E1: 别名全链路 ──
        # 注：create_alias 在独立进程调用，其 invalidate_cache 作用不到服务进程，
        # 服务侧别名缓存 15s TTL 到期后生效 —— 等 16s 验证改写
        alias = await create_alias(AliasCreate(alias="smoke-alias", target=model_name), db)
        print("  · 别名已创建，等 16s 服务端缓存过期...")
        await asyncio.sleep(16)
        st3, resp3 = http_json("/v1/chat/completions",
                               {"model": "smoke-alias", "messages": [{"role": "user", "content": "reply ok"}],
                                "max_tokens": 5}, key=master_key)
        check("E1 别名请求改写", st3 == 200, f"status={st3} routed={resp3.get('model', '')}")
        await delete_alias(alias["id"], db)

        # ── B4: 一键诊断（对上一次成功的模型）──
        from server.models.model import Model
        from server.models.provider import Provider
        mrow = (await db.execute(
            text("SELECT m.id FROM models m JOIN providers p ON p.id=m.provider_id "
                 "WHERE p.name=:pn AND m.model_id=:mn"),
            {"pn": row[1], "mn": row[2]})).first()
        if mrow:
            # 独立进程没有 health_checker 单例：补一个再调（服务内自动就绪）
            import server.main as _main
            from server.core.health_checker import HealthChecker
            if _main.get_health_checker() is None:
                _main._health_checker = HealthChecker()
            diag = await diagnose(type("B", (), {"model_id": mrow[0], "combo_id": None, "max_targets": 6})(), db)
            r0 = diag["results"][0]
            check("B4 一键诊断", diag["total"] == 1 and r0["latency_ms"] >= 0,
                  f"status={r0['status']} latency={r0['latency_ms']}ms")
        else:
            check("B4 一键诊断", False, "model row not found")

        # ── B1/B2/D4 ──
        ls = await live_summary(db)
        check("B1 实时摘要", ls["today"]["requests"] >= 2 and isinstance(ls["cooling"], list),
              f"today_req={ls['today']['requests']} recent={len(ls['recent'])} cooling={len(ls['cooling'])}")
        fa = await failure_analysis(24, db)
        check("B2 失败分析", isinstance(fa["by_error_type"], list), f"errors={fa['total_errors']}")
        ph = await price_health(db)
        check("D4 价格健康", ph["missing_count"] + ph["zero_count"] >= 0,
              f"missing={ph['missing_count']} zero={ph['zero_count']}")

    # 清理网关密钥
    async with AsyncSessionLocal() as db:
        await delete_gateway_key(gk_id, db)
        print("  · 冒烟网关密钥已删除")

    # ── C3: 立即备份 ──
    r = await run_backup("smoke")
    check("C3 立即备份", r["ok"] and r["size"] > 0, f"file={r.get('file')} size={r.get('size')}")
    check("C3 备份列表", len(list_backups()) >= 1, f"n={len(list_backups())}")

    print(f"\n== 冒烟结果 == 通过 {ok} / 失败 {fail}")
    sys.exit(1 if fail else 0)


asyncio.run(main())
