"""Admin API for historical routing decisions and fallback chains."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import AsyncSessionLocal
from server.models.route_decision import RouteDecision


router = APIRouter(prefix="/admin/api/route-decisions", tags=["route-decisions"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


def _serialize(row: RouteDecision, *, detail: bool = False) -> dict:
    data = {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "requested_model": row.requested_model,
        "route_type": row.route_type,
        "strategy": row.strategy,
        "stream": bool(row.stream),
        "status": row.status,
        "selected_provider": row.selected_provider,
        "selected_model": row.selected_model,
        "selection_reason": row.selection_reason,
        "failure_reason": row.failure_reason,
        "candidate_count": row.candidate_count or 0,
        "attempt_count": row.attempt_count or 0,
        "fallback_count": row.fallback_count or 0,
        "estimated_tokens": row.estimated_tokens,
        "decision_ms": row.decision_ms,
        "total_latency_ms": row.total_latency_ms,
        "ttft_ms": row.ttft_ms,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }
    if detail:
        data["candidates"] = row.candidates or []
        data["attempts"] = row.attempts or []
    return data


@router.get("")
async def list_route_decisions(
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    route_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=100),
    fallback_only: bool = Query(False),
    window_hours: int = Query(24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
):
    since = _utcnow() - timedelta(hours=window_hours)
    filters = [RouteDecision.created_at >= since]
    if route_type:
        filters.append(RouteDecision.route_type == route_type)
    if status:
        filters.append(RouteDecision.status == status)
    if fallback_only:
        filters.append(RouteDecision.fallback_count > 0)
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(or_(
            RouteDecision.conversation_id.like(pattern),
            RouteDecision.requested_model.like(pattern),
            RouteDecision.selected_provider.like(pattern),
            RouteDecision.selected_model.like(pattern),
        ))

    total = (await db.execute(
        select(func.count(RouteDecision.id)).where(*filters)
    )).scalar_one() or 0
    rows = (await db.execute(
        select(RouteDecision)
        .where(*filters)
        .order_by(RouteDecision.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all()

    summary_row = (await db.execute(select(
        func.count(RouteDecision.id),
        func.coalesce(func.sum(case((RouteDecision.status == "success", 1), else_=0)), 0),
        func.coalesce(func.sum(case((RouteDecision.fallback_count > 0, 1), else_=0)), 0),
        func.avg(RouteDecision.decision_ms),
        func.avg(RouteDecision.total_latency_ms),
        func.avg(RouteDecision.ttft_ms),
    ).where(RouteDecision.created_at >= since))).one()
    count, successes, fallback_decisions, avg_decision, avg_total, avg_ttft = summary_row
    count = int(count or 0)

    return {
        "items": [_serialize(row) for row in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "summary": {
            "window_hours": window_hours,
            "total": count,
            "success_rate": round(int(successes or 0) / (count or 1) * 100, 1),
            "fallback_rate": round(int(fallback_decisions or 0) / (count or 1) * 100, 1),
            "avg_decision_ms": round(float(avg_decision), 1) if avg_decision is not None else None,
            "avg_total_latency_ms": round(float(avg_total), 1) if avg_total is not None else None,
            "avg_ttft_ms": round(float(avg_ttft), 1) if avg_ttft is not None else None,
        },
    }


@router.get("/{decision_id}")
async def get_route_decision(decision_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(RouteDecision, decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Routing decision not found")
    return _serialize(row, detail=True)
