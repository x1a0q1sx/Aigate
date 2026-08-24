import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core import route_decision as trace
from server.models.route_decision import RouteDecision
from server.api.route_decisions_router import get_route_decision, list_route_decisions


@pytest.mark.asyncio
async def test_route_decision_persists_ranked_candidates_and_fallbacks(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(RouteDecision.__table__.create)

    import server.db
    monkeypatch.setattr(server.db, "AsyncSessionLocal", session_factory)

    conversation_id = "decision-test-1"
    trace.begin_decision(conversation_id, "auto", "auto", stream=True, estimated_tokens=321)
    trace.capture_candidates(conversation_id, [
        {
            "rank": 1,
            "model_pk": 10,
            "provider": "primary",
            "model": "model-a",
            "final_score": 91.2,
            "speed_score": 80,
            "intel_score": 94,
            "stability_score": 88,
            "eligible": True,
            "api_key": "must-not-be-stored",
        },
        {
            "rank": 2,
            "model_pk": 11,
            "provider": "backup",
            "model": "model-b",
            "final_score": 84.5,
            "eligible": True,
        },
    ])
    trace.mark_selected(
        conversation_id,
        provider="primary",
        model="model-a",
        model_pk=10,
        reason="highest ranked available candidate",
    )
    trace.add_attempt(
        conversation_id,
        provider="primary",
        model="model-a",
        status="failed",
        attempt=0,
        latency_ms=1200,
        error="timeout",
    )
    trace.mark_selected(
        conversation_id,
        provider="backup",
        model="model-b",
        model_pk=11,
        reason="next ranked candidate",
    )
    trace.add_attempt(
        conversation_id,
        provider="backup",
        model="model-b",
        status="success",
        attempt=1,
        latency_ms=800,
        ttft_ms=300,
    )
    await trace.finish_decision(
        conversation_id,
        status="success",
        provider="backup",
        model="model-b",
        fallback_count=1,
        total_latency_ms=2000,
        ttft_ms=300,
    )

    async with session_factory() as session:
        row = (await session.execute(select(RouteDecision))).scalar_one()
    assert row.status == "success"
    assert row.selected_provider == "backup"
    assert row.selected_model == "model-b"
    assert row.candidate_count == 2
    assert row.attempt_count == 2
    assert row.fallback_count == 1
    assert row.candidates[0]["final_score"] == 91.2
    assert "api_key" not in row.candidates[0]
    assert row.attempts[0]["error"] == "timeout"
    assert trace.active_decision_count() == 0
    await engine.dispose()


def test_route_decision_caps_and_truncates_diagnostic_data():
    conversation_id = "decision-test-2"
    trace.begin_decision(conversation_id, "auto", "auto")
    for index in range(80):
        trace.capture_candidates(conversation_id, [{"model_pk": index, "model": f"m-{index}"}])
    for index in range(50):
        trace.add_attempt(conversation_id, status="failed", attempt=index, error="x" * 1000)

    active = trace._active.pop(conversation_id)
    assert len(active.candidates) == 50
    assert len(active.attempts) == 30
    assert len(active.attempts[0]["error"]) == 500


@pytest.mark.asyncio
async def test_route_decision_admin_api_filters_and_summarizes():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(RouteDecision.__table__.create)
    async with session_factory() as session:
        session.add_all([
            RouteDecision(
                conversation_id="api-1",
                requested_model="auto",
                route_type="auto",
                status="success",
                selected_provider="p1",
                selected_model="m1",
                fallback_count=1,
                decision_ms=20,
                total_latency_ms=1000,
                candidates=[],
                attempts=[],
            ),
            RouteDecision(
                conversation_id="api-2",
                requested_model="p2/m2",
                route_type="direct",
                status="error",
                fallback_count=0,
                decision_ms=10,
                total_latency_ms=500,
                candidates=[],
                attempts=[],
            ),
        ])
        await session.commit()

        result = await list_route_decisions(
            page=1,
            page_size=30,
            route_type="auto",
            status=None,
            q=None,
            fallback_only=True,
            window_hours=24,
            db=session,
        )
        assert result["total"] == 1
        assert result["items"][0]["conversation_id"] == "api-1"
        assert result["summary"]["total"] == 2
        assert result["summary"]["success_rate"] == 50.0
        assert result["summary"]["fallback_rate"] == 50.0

        detail = await get_route_decision(result["items"][0]["id"], db=session)
        assert detail["candidates"] == []
        assert detail["attempts"] == []
    await engine.dispose()
