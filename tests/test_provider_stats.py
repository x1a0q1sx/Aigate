from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api.admin_router import get_provider_model_stats
from server.api.v1_router import _is_stream_content_validation_error
from server.models.model import Model
from server.models.provider import Provider


@pytest.mark.asyncio
async def test_provider_model_stats_returns_lightweight_visible_counts(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Provider.__table__.create)
        await connection.run_sync(Model.__table__.create)

    async with session_factory() as session:
        enabled_provider = Provider(name="enabled", base_url="https://example.com")
        disabled_provider = Provider(name="disabled", base_url="https://example.com", enabled=False)
        session.add_all([enabled_provider, disabled_provider])
        await session.flush()

        session.add_all([
            Model(provider_id=enabled_provider.id, model_id="active"),
            Model(provider_id=enabled_provider.id, model_id="hidden", enabled=False),
            Model(provider_id=disabled_provider.id, model_id="unavailable"),
        ])
        await session.commit()

        checker = SimpleNamespace(_fail_count={1: 3, 2: 7, 999: 11})
        import server.main as server_main
        monkeypatch.setattr(server_main, "get_health_checker", lambda: checker)

        stats = await get_provider_model_stats(db=session)

    assert stats == [
        {"provider_id": enabled_provider.id, "model_count": 1, "fail_count": 3}
    ]
    await engine.dispose()


def test_upstream_stream_validation_errors_are_classified():
    errors = [
        "stream disconnected before completion: idle timeout waiting for SSE",
        "Stream content validation failed: received 0 chars but content is insufficient",
    ]

    assert all(_is_stream_content_validation_error(error) for error in errors)
    assert not _is_stream_content_validation_error("connection reset")
