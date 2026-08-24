"""In-request routing trace collection and persistence.

The active trace only contains routing metadata. Request content and credentials are
deliberately excluded so the decision center is safe to retain and inspect.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional


_MAX_CANDIDATES = 50
_MAX_ATTEMPTS = 30
_MAX_ERROR = 500
_ACTIVE_TTL_SECONDS = 3600
_CANDIDATE_FIELDS = {
    "rank", "model_pk", "provider", "model", "eligible", "selected",
    "selection_reason", "skip_reason", "final_score", "speed_score",
    "intel_score", "intel_source", "stability_score", "avg_latency_ms",
    "success_rate", "priority_boost", "health",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _short(value: Any, limit: int = _MAX_ERROR) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


@dataclass
class ActiveRouteDecision:
    conversation_id: str
    requested_model: str
    route_type: str
    stream: bool
    estimated_tokens: Optional[int] = None
    strategy: Optional[str] = None
    started_monotonic: float = field(default_factory=time.monotonic)
    created_at: datetime = field(default_factory=_utcnow)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    selected_provider: Optional[str] = None
    selected_model: Optional[str] = None
    selection_reason: Optional[str] = None
    selected_monotonic: Optional[float] = None


_active: Dict[str, ActiveRouteDecision] = {}


def _cleanup_stale() -> None:
    now = time.monotonic()
    stale = [key for key, item in _active.items() if now - item.started_monotonic > _ACTIVE_TTL_SECONDS]
    for key in stale:
        _active.pop(key, None)


def begin_decision(
    conversation_id: str,
    requested_model: str,
    route_type: str,
    *,
    stream: bool = False,
    estimated_tokens: Optional[int] = None,
    strategy: Optional[str] = None,
) -> ActiveRouteDecision:
    _cleanup_stale()
    trace = ActiveRouteDecision(
        conversation_id=conversation_id,
        requested_model=requested_model or "unknown",
        route_type=route_type,
        stream=bool(stream),
        estimated_tokens=estimated_tokens,
        strategy=strategy,
    )
    _active[conversation_id] = trace
    return trace


def configure_decision(
    conversation_id: str,
    *,
    route_type: Optional[str] = None,
    strategy: Optional[str] = None,
    estimated_tokens: Optional[int] = None,
) -> None:
    trace = _active.get(conversation_id)
    if not trace:
        return
    if route_type:
        trace.route_type = route_type
    if strategy is not None:
        trace.strategy = strategy
    if estimated_tokens is not None:
        trace.estimated_tokens = int(estimated_tokens)


def capture_candidates(conversation_id: str, candidates: Iterable[Dict[str, Any]]) -> None:
    trace = _active.get(conversation_id)
    if not trace:
        return

    def candidate_key(item: Dict[str, Any]):
        model_pk = item.get("model_pk")
        return ("id", model_pk) if model_pk is not None else (
            "name", item.get("provider"), item.get("model")
        )

    existing = {candidate_key(item): index for index, item in enumerate(trace.candidates)}
    for raw in candidates:
        item = {key: value for key, value in dict(raw).items() if key in _CANDIDATE_FIELDS}
        key = candidate_key(item)
        if key in existing:
            current = trace.candidates[existing[key]]
            current.update({k: v for k, v in item.items() if v is not None})
            continue
        if len(trace.candidates) >= _MAX_CANDIDATES:
            break
        existing[key] = len(trace.candidates)
        trace.candidates.append(item)


def mark_candidate_skipped(
    conversation_id: str,
    *,
    model_pk: Optional[int] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    reason: str,
) -> None:
    trace = _active.get(conversation_id)
    if not trace:
        return
    for item in trace.candidates:
        if model_pk is not None and item.get("model_pk") == model_pk:
            item["eligible"] = False
            item["skip_reason"] = _short(reason, 255)
            return
        if provider and model and item.get("provider") == provider and item.get("model") == model:
            item["eligible"] = False
            item["skip_reason"] = _short(reason, 255)
            return
    capture_candidates(conversation_id, [{
        "model_pk": model_pk,
        "provider": provider,
        "model": model,
        "eligible": False,
        "skip_reason": _short(reason, 255),
    }])


def mark_selected(
    conversation_id: str,
    *,
    provider: Optional[str],
    model: Optional[str],
    model_pk: Optional[int] = None,
    reason: str = "highest_ranked_available",
) -> None:
    trace = _active.get(conversation_id)
    if not trace:
        return
    trace.selected_provider = provider
    trace.selected_model = model
    trace.selection_reason = _short(reason, 255)
    if trace.selected_monotonic is None:
        trace.selected_monotonic = time.monotonic()
    for item in trace.candidates:
        item.pop("selected", None)
        item.pop("selection_reason", None)
    for item in trace.candidates:
        if (model_pk is not None and item.get("model_pk") == model_pk) or (
            provider and model and item.get("provider") == provider and item.get("model") == model
        ):
            item["selected"] = True
            item["selection_reason"] = trace.selection_reason
            return


def add_attempt(
    conversation_id: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    status: str,
    attempt: Optional[int] = None,
    latency_ms: Optional[int] = None,
    ttft_ms: Optional[int] = None,
    error: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    trace = _active.get(conversation_id)
    if not trace or len(trace.attempts) >= _MAX_ATTEMPTS:
        return
    normalized_attempt = int(attempt if attempt is not None else len(trace.attempts))
    normalized_error = _short(error)
    for existing in trace.attempts:
        if (
            existing.get("attempt") == normalized_attempt
            and existing.get("provider") == provider
            and existing.get("model") == model
            and existing.get("status") == status
            and existing.get("error") == normalized_error
        ):
            if existing.get("latency_ms") is None and latency_ms is not None:
                existing["latency_ms"] = int(latency_ms)
            if existing.get("ttft_ms") is None and ttft_ms is not None:
                existing["ttft_ms"] = int(ttft_ms)
            return
    entry = {
        "attempt": normalized_attempt,
        "provider": provider,
        "model": model,
        "status": status,
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
        "ttft_ms": int(ttft_ms) if ttft_ms is not None else None,
        "error": normalized_error,
        "reason": _short(reason, 255),
    }
    trace.attempts.append({key: value for key, value in entry.items() if value is not None})


def ingest_attempt_errors(conversation_id: str, attempts: Optional[Iterable[Dict[str, Any]]]) -> None:
    trace = _active.get(conversation_id)
    if not trace or not attempts:
        return
    known = {
        (entry.get("attempt"), entry.get("provider"), entry.get("model"), entry.get("error"))
        for entry in trace.attempts
    }
    for raw in attempts:
        target = raw.get("model") or raw.get("target")
        provider = None
        model = target
        if isinstance(target, str) and "/" in target:
            provider, model = target.split("/", 1)
        key = (raw.get("attempt"), provider, model, _short(raw.get("error")))
        if key in known:
            continue
        add_attempt(
            conversation_id,
            provider=provider,
            model=model,
            status="skipped" if str(raw.get("error", "")).startswith("skip") else "failed",
            attempt=raw.get("attempt"),
            error=raw.get("error"),
        )
        known.add(key)


async def finish_decision(
    conversation_id: str,
    *,
    status: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    fallback_count: Optional[int] = None,
    total_latency_ms: Optional[int] = None,
    ttft_ms: Optional[int] = None,
    failure_reason: Optional[str] = None,
    attempts: Optional[Iterable[Dict[str, Any]]] = None,
) -> None:
    trace = _active.get(conversation_id)
    if not trace:
        return
    ingest_attempt_errors(conversation_id, attempts)
    trace = _active.pop(conversation_id, None)
    if not trace:
        return

    completed_at = _utcnow()
    elapsed_ms = int((time.monotonic() - trace.started_monotonic) * 1000)
    decision_ms = None
    if trace.selected_monotonic is not None:
        decision_ms = int((trace.selected_monotonic - trace.started_monotonic) * 1000)
    selected_provider = provider or trace.selected_provider
    selected_model = model or trace.selected_model
    fallback = fallback_count
    if fallback is None:
        tried = [entry for entry in trace.attempts if entry.get("status") in {"success", "failed"}]
        fallback = max(0, len(tried) - 1)

    try:
        from server.db import AsyncSessionLocal
        from server.models.route_decision import RouteDecision
        from sqlalchemy import delete

        async with AsyncSessionLocal() as db:
            await db.execute(delete(RouteDecision).where(
                RouteDecision.created_at < _utcnow() - timedelta(days=30)
            ))
            db.add(RouteDecision(
                conversation_id=trace.conversation_id,
                requested_model=trace.requested_model,
                route_type=trace.route_type,
                strategy=trace.strategy,
                stream=trace.stream,
                status=status,
                selected_provider=selected_provider,
                selected_model=selected_model,
                selection_reason=trace.selection_reason,
                failure_reason=_short(failure_reason),
                candidate_count=len(trace.candidates),
                attempt_count=len(trace.attempts),
                fallback_count=int(fallback or 0),
                estimated_tokens=trace.estimated_tokens,
                decision_ms=decision_ms,
                total_latency_ms=int(total_latency_ms) if total_latency_ms is not None else elapsed_ms,
                ttft_ms=int(ttft_ms) if ttft_ms is not None else None,
                candidates=trace.candidates,
                attempts=trace.attempts,
                created_at=trace.created_at,
                completed_at=completed_at,
            ))
            await db.commit()
    except Exception as exc:
        print(f"[WARN] routing decision write failed: {type(exc).__name__}: {exc}", flush=True)


def active_decision_count() -> int:
    _cleanup_stale()
    return len(_active)
