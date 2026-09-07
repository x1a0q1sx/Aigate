"""D1/D2: 下游网关密钥校验 + RPM 限速 + 每日预算。

verify_aigate_api_key（v1/anthropic/responses 三协议入口共用）在主密钥
未命中时走这里：
- 命中 enabled 的 GatewayKey → 记 RPM、查预算、写请求上下文（日志关联）
- 未命中 → 返回 None（调用方决定 401 / 开放模式）

预算口径：request_logs 按 downstream_key_id + UTC 日聚合
prompt+completion(+cache) token 与 estimated_cost_usd。
"""
import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, update

from ..db import AsyncSessionLocal
from ..models.gateway_key import GatewayKey
from ..models.request_log import RequestLog

_KEY_PREFIX = "gk-"
# RPM 滑动窗口（进程内；单进程部署足够，多 worker 场景退化为尽力限速）
_rpm_windows = defaultdict(deque)


def generate_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_rpm(key: GatewayKey) -> None:
    """进程内滑动窗口限速。超限抛 429。"""
    limit = key.rpm_limit
    if not limit or limit <= 0:
        return
    now = time.monotonic()
    win = _rpm_windows[key.id]
    while win and now - win[0] > 60:
        win.popleft()
    if len(win) >= limit:
        raise _err(429, f"Gateway key '{key.name or key.id}' RPM limit exceeded ({limit}/min)")
    win.append(now)


def _err(status: int, detail: str):
    from fastapi import HTTPException
    return HTTPException(status_code=status, detail=detail)


async def check_gateway_key(token: str) -> Optional[dict]:
    """校验网关密钥。返回 {id, name, budget, over_budget}；未知 key 返回 None。"""
    h = hash_key(token)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(GatewayKey).where(GatewayKey.key_hash == h)
        )).scalar_one_or_none()
        if not row:
            return None
        if not row.enabled:
            raise _err(403, f"Gateway key '{row.name or row.id}' is disabled")
        if row.expires_at and row.expires_at < datetime.utcnow():
            raise _err(403, f"Gateway key '{row.name or row.id}' is expired")
        _check_rpm(row)
        budget = await check_budget(db, row.id)
        over = budget["over_token"] or budget["over_cost"]
        if over:
            try:
                from server.core.notifier import notify_event
                notify_event("budget",
                             f"网关密钥 '{row.name or row.id}' 今日预算超限："
                             f"tokens {budget['tokens']}/{row.daily_token_limit or '-'}"
                             f"，cost ${budget['cost']:.4f}/${row.daily_cost_limit_usd or '-'}")
            except Exception:
                pass
        if over and (row.over_limit_action or "reject") != "warn":
            raise _err(429, (
                f"Gateway key '{row.name or row.id}' daily budget exceeded "
                f"(tokens {budget['tokens']}/{row.daily_token_limit or '-'}"
                f", cost ${budget['cost']:.4f}/${row.daily_cost_limit_usd or '-'})"
            ))
        try:  # last_used 尽力更新，失败不影响鉴权
            await db.execute(update(GatewayKey).where(GatewayKey.id == row.id)
                             .values(last_used_at=datetime.utcnow()))
            await db.commit()
        except Exception:
            await db.rollback()
        return {"id": row.id, "name": row.name, "budget": budget, "over_budget": over}


async def check_budget(db, key_id: int) -> dict:
    """该 key 今日（UTC）已用 token 与成本。"""
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    row = (await db.execute(
        select(
            func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
            func.coalesce(func.sum(RequestLog.completion_tokens), 0),
            func.coalesce(func.sum(RequestLog.cache_read_tokens), 0),
            func.coalesce(func.sum(RequestLog.cache_write_tokens), 0),
            func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0),
            func.count(RequestLog.id),
        ).where(
            RequestLog.downstream_key_id == key_id,
            RequestLog.created_at >= day_start,
            RequestLog.is_health_check.is_(False),
        )
    )).first()
    pt, ct, crt, cwt, cost, reqs = row
    tokens = int(pt or 0) + int(ct or 0) + int(crt or 0) + int(cwt or 0)
    cost = float(cost or 0)
    key = (await db.execute(
        select(GatewayKey.daily_token_limit, GatewayKey.daily_cost_limit_usd)
        .where(GatewayKey.id == key_id)
    )).first()
    t_limit, c_limit = (key if key else (None, None))
    return {
        "tokens": tokens,
        "cost": round(cost, 6),
        "requests": int(reqs or 0),
        "token_limit": t_limit,
        "cost_limit": c_limit,
        "over_token": bool(t_limit and tokens >= t_limit),
        "over_cost": bool(c_limit and cost >= c_limit),
    }
