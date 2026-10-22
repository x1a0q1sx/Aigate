"""用量聚合（单一实现，供分析页 / headroom 共用）。

背景（2026-09-24）：`routed_provider_id` 是后加的列，早期写入路径只写了服务商
**名称**（直连流式最典型——生产实测占当日 95%：786/832 行有 ttft 但 id 为 NULL）。
而聚合查询此前一律 `WHERE routed_provider_id IS NOT NULL` → 这些行被整体丢弃：

  - 分析页「按服务商用量」只显示走 combo/auto 的少数请求（用户报告的显示不全）
  - headroom 每日限额统计不到真实用量 → 保留额度形同虚设（功能性缺陷）

这里统一按「id 优先、名称兜底」聚合：先加载 providers 名称→id 映射，把只有名称的
历史行归到正确服务商。健康检查行与 pending（在途）行不计入——前者不是业务调用，
后者尚未确定服务商且会在完成时原位更新，提前计入会虚增。

`provider_id=None` 的行有两种截然不同的来历，用 `orphan_kind` 区分（2026-10-21）：
  - `"unattributed"`：日志里连服务商**名称**都没有——请求在路由/拿到上游之前就
    结束了（route_error、pending 超时被清扫成 interrupted 等）。生产上它每日占
    几百行（10-20 实测 725 行），其中 99% 是 interrupted 且 0 token。
  - `"deleted"`：日志里写了名称，但当前 providers 表里找不到——服务商确实被删除
    或改名了，保留原名称便于排查。
此前两者共用 "(unknown)" 一个桶，前端统一标注「（已删除）」，把大量**路由前失败**
的请求误报成「服务商已删除」，误导排查方向。

2026-10-20 扩展三种视角（分析页「用量分析」切换）：
  - `provider`：只有服务商（原有行为，headroom 依赖它）
  - `provider_model`：服务商 × 实际路由模型（routed_model，不是请求模型）
  - `provider_account`：服务商 × OAuth 账号（routed_account；非 OAuth 服务商为 None）
"""
from __future__ import annotations
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models.provider import Provider
from server.models.request_log import RequestLog

# 允许的聚合视角；未知值一律退回 provider（避免动态列拼接）
USAGE_DIMENSIONS = ("provider", "provider_model", "provider_account")


def _tokens_sum():
    return func.coalesce(func.sum(
        func.coalesce(RequestLog.prompt_tokens, 0)
        + func.coalesce(RequestLog.completion_tokens, 0)), 0)


async def aggregate_usage(
    db: AsyncSession,
    *,
    since: datetime,
    until: Optional[datetime] = None,
    dimension: str = "provider",
    provider_names: Optional[Dict[int, str]] = None,
) -> List[Dict]:
    """按指定视角聚合 [since, until] 窗口内的请求数 / token / 成本。

    返回按 token 降序的列表，每项：
      {"provider_id": int|None, "provider_name": str, "orphan_kind": str|None,
       "requests": int, "tokens": int, "cost_usd": float,
       "model": str|None (仅 provider_model), "account": str|None (仅 provider_account)}

    orphan_kind: 已归属服务商的行 None；`provider_id is None` 的行为
    "deleted"（服务商已删除/改名）或 "unattributed"（路由前就失败，无名称）。

    provider_names: 可选的 {id: name} 预加载缓存（批量调用时避免重复查询）。
    """
    dim = dimension if dimension in USAGE_DIMENSIONS else "provider"
    conds = [RequestLog.created_at >= since,
             RequestLog.is_health_check.is_(False)]
    if until is not None:
        conds.append(RequestLog.created_at <= until)
    # pending 行在途：无服务商归属且完成时会原位更新，不计入用量
    conds.append(RequestLog.status.isnot("pending"))

    group_cols = [RequestLog.routed_provider_id, RequestLog.routed_provider]
    sub_col = None
    if dim == "provider_model":
        sub_col = RequestLog.routed_model
    elif dim == "provider_account":
        sub_col = RequestLog.routed_account
    if sub_col is not None:
        group_cols.append(sub_col)

    rows = (await db.execute(
        select(
            *group_cols,
            func.count(RequestLog.id),
            _tokens_sum(),
            func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
        ).where(*conds)
        .group_by(*group_cols)
    )).all()

    # 名称→id 兜底映射（只取一次，全表扫描成本与 providers 行数同阶）
    if provider_names is None:
        prov_rows = (await db.execute(select(Provider.id, Provider.name))).all()
        provider_names = {int(r[0]): r[1] for r in prov_rows}
    name_to_id = {name: pid for pid, name in provider_names.items()}

    merged: Dict[object, Dict] = {}
    for row in rows:
        pid, pname = row[0], row[1]
        sub = row[2] if sub_col is not None else None
        reqs, toks, cost = row[-3], row[-2], row[-1]
        rid = pid if pid is not None else name_to_id.get(pname or "")
        # 无 id 且名称也匹配不到 → 未归属桶；按有无名称区分「压根没路由到」与「已删除」
        key = (("id", rid) if rid is not None else ("name", pname or ""), sub or "")
        item = merged.get(key)
        if item is None:
            if rid is not None:
                display, kind = provider_names.get(rid, pname), None
            elif pname:
                display, kind = pname, "deleted"   # 有名称但表里没有：删了/改名了
            else:
                display, kind = "(未归属)", "unattributed"  # 路由前就结束，无从归属
            item = merged[key] = {"provider_id": rid, "provider_name": display or "(未归属)",
                                  "orphan_kind": kind,
                                  "requests": 0, "tokens": 0, "cost_usd": 0.0}
            if dim == "provider_model":
                item["model"] = sub
            elif dim == "provider_account":
                item["account"] = sub
        item["requests"] += int(reqs or 0)
        item["tokens"] += int(toks or 0)
        item["cost_usd"] += float(cost or 0)

    items = list(merged.values())
    for it in items:
        it["cost_usd"] = round(it["cost_usd"], 4)
    # 主序 token 降序：模型/账号视角下「谁最烧 token」最需要一眼看到
    items.sort(key=lambda x: (-x["tokens"], x["provider_name"], str(x.get("model") or x.get("account") or "")))
    return items


async def aggregate_usage_by_provider(
    db: AsyncSession,
    *,
    since: datetime,
    until: Optional[datetime] = None,
    provider_names: Optional[Dict[int, str]] = None,
) -> List[Dict]:
    """按服务商聚合（headroom / 服务商页沿用的稳定入口）。"""
    return await aggregate_usage(db, since=since, until=until,
                                 dimension="provider", provider_names=provider_names)
