"""Persistent, privacy-safe snapshots of routing decisions."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, JSON, String, Text

from .base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RouteDecision(Base):
    __tablename__ = "routing_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(64), nullable=False, unique=True)
    requested_model = Column(String(200), nullable=False)
    route_type = Column(String(20), nullable=False, default="direct")
    strategy = Column(String(40), nullable=True)
    stream = Column(Boolean, nullable=False, default=False, server_default="0")
    status = Column(String(20), nullable=False, default="running")

    selected_provider = Column(String(100), nullable=True)
    selected_model = Column(String(200), nullable=True)
    selection_reason = Column(String(255), nullable=True)
    failure_reason = Column(Text, nullable=True)

    candidate_count = Column(Integer, nullable=False, default=0, server_default="0")
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    fallback_count = Column(Integer, nullable=False, default=0, server_default="0")
    estimated_tokens = Column(Integer, nullable=True)
    decision_ms = Column(Integer, nullable=True)
    total_latency_ms = Column(Integer, nullable=True)
    ttft_ms = Column(Integer, nullable=True)

    candidates = Column(JSON, nullable=False, default=list)
    attempts = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)


Index("idx_routing_decisions_created", RouteDecision.created_at)
Index("idx_routing_decisions_status_created", RouteDecision.status, RouteDecision.created_at)
Index("idx_routing_decisions_type_created", RouteDecision.route_type, RouteDecision.created_at)
