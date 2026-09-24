"""按服务商聚合用量（单一实现，供分析页 / headroom 共用）。

背景（2026-09-24）：`routed_provider_id` 是后加的列，早期写入路径只写了服务商
**名称**（直连流式最典型——生产实测占当日 95%：786/832 行有 ttft 但 id 为 NULL）。
而聚合查询此前一律 `WHERE routed_provider_id IS NOT NULL` → 这些行被整体丢弃：

  - 分析页「按服务商用量」只显示走 combo/auto 的少数请求（用户报告的显示不全）
  - headroom 每日限额统计不到真实用量 → 保留额度形同虚设（功能性缺陷）

这里统一按「id 优先、名称兜底」聚合：先加载 providers 名称→id 映射，把只有名称的
历史行归到正确服务商；名称对不上任何服务商（已删除/改名）才落 `provider_id=None`
的 "(unknown)" 桶。健康检查行与 pending（在途）行不计入——前者不是业务调用，
后者尚未确定服务商且会在完成时原位更新，提前计入会虚增。
"""
from __future__ import annotations
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models.provider import Provider
from server.models.request_log import RequestLog


async def aggregate_usage_by_provider(
    db: AsyncSession,
    *,
    since: datetime,
    until: Optional[datetime] = None,
    provider_names: Optional[Dict[int, str]] = None,
) -> List[Dict]:
    """按服务商聚合 [since, until] 窗口内的请求数 / token / 成本。

    返回按 token 降序的列表：
      [{"provider_id": int|None, "provider_name": str,
        "requests": int, "tokens": int, "cost_usd": float}, ...]

    provider_names: 可选的 {id: name} 预加载缓存（批量调用时避免重复查询）。
    """
    conds = [RequestLog.created_at >= since,
             RequestLog.is_health_check.is_(False)]
    if until is not None:
        conds.append(RequestLog.created_at <= until)
    # pending 行在途：无服务商归属且完成时会原位更新，不计入用量
    conds.append(RequestLog.status.isnot("pending"))

    rows = (await db.execute(
        select(
            RequestLog.routed_provider_id,
            RequestLog.routed_provider,
            func.count(RequestLog.id),
            func.coalesce(func.sum(
                func.coalesce(RequestLog.prompt_tokens, 0)
                + func.coalesce(RequestLog.completion_tokens, 0)), 0),
            func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
        ).where(*conds)
        .group_by(RequestLog.routed_provider_id, RequestLog.routed_provider)
    )).all()

    # 名称→id 兜底映射（只取一次，全表扫描成本与 providers 行数同阶）
    if provider_names is None:
        prov_rows = (await db.execute(select(Provider.id, Provider.name))).all()
        provider_names = {int(r[0]): r[1] for r in prov_rows}
    name_to_id = {name: pid for pid, name in provider_names.items()}

    merged: Dict[object, Dict] = {}
    for pid, pname, reqs, toks, cost in rows:
        rid = pid if pid is not None else name_to_id.get(pname or "")
        # 无 id 且名称也匹配不到（服务商已删除）→ 归入 unknown 桶，但保留原始名称
        key = ("id", rid) if rid is not None else ("name", pname or "")
        item = merged.get(key)
        if item is None:
            display = provider_names.get(rid, pname) if rid is not None else (pname or "(unknown)")
            # 已删除服务商（匹配不到 id）优先显示日志里的名称，便于排查
            item = merged[key] = {"provider_id": rid, "provider_name": display or "(unknown)",
                                  "requests": 0, "tokens": 0, "cost_usd": 0.0}
        item["requests"] += int(reqs or 0)
        item["tokens"] += int(toks or 0)
        item["cost_usd"] += float(cost or 0)

    items = list(merged.values())
    for it in items:
        it["cost_usd"] = round(it["cost_usd"], 4)
    items.sort(key=lambda x: x["tokens"], reverse=True)
    return items
