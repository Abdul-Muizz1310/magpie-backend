"""Tests for the async scrape + run-status endpoints.

Uses FastAPI ``dependency_overrides`` to inject a SQLite-backed session
factory; the Procrastinate task is stubbed so no real queue is needed.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from magpie.api.deps import get_db_session, get_session_factory_dep
from magpie.config.schema import SourceConfig
from magpie.main import app
from magpie.storage.models import SourceOrigin
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository


def _cfg(name: str = "src") -> SourceConfig:
    return SourceConfig(
        name=name,
        url="https://example.com",  # type: ignore[arg-type]
        schedule="0 */6 * * *",
        item={  # type: ignore[arg-type]
            "container": "tr.row",
            "fields": [
                {"name": "id", "selector": "::attr(id)"},
                {"name": "title", "selector": "a::text"},
            ],
            "dedupe_key": "id",
        },
    )


@pytest.fixture
async def client(session_factory):
    async def _factory_override():
        return session_factory

    async def _session_override():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session_factory_dep] = _factory_override
    app.dependency_overrides[get_db_session] = _session_override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def seeded_source(session_factory):
    cfg = _cfg()
    async with session_factory() as session:
        await SourcesRepository(session).create(
            config=cfg,
            origin=SourceOrigin.api,
            yaml_text=yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False),
        )
        await session.commit()
    return cfg


class TestEnqueueEndpoint:
    async def test_enqueue_creates_queued_run_and_defers_task(
        self, client: AsyncClient, session_factory, seeded_source
    ) -> None:
        with patch(
            "magpie.api.routers.jobs.scrape_source_task.defer_async",
            new=AsyncMock(return_value=42),
        ) as mock_defer:
            resp = await client.post("/api/scrape/src/enqueue", json={"max_items": 5})

        assert resp.status_code == 202
        body = resp.json()
        assert body["source"] == "src"
        assert body["status"] == "queued"
        assert uuid.UUID(body["run_id"])

        # Task was deferred with the new run id.
        kwargs = mock_defer.call_args.kwargs
        assert kwargs["source"] == "src"
        assert kwargs["max_items"] == 5
        assert kwargs["run_id"] == body["run_id"]

        # A queued run row exists for this run id.
        async with session_factory() as session:
            repo = PgRunRepository(session)
            run = await repo.get(uuid.UUID(body["run_id"]))
            assert run is not None
            assert run.status.value == "queued"
            assert run.job_id == "42"

    async def test_enqueue_unknown_source_404(self, client: AsyncClient) -> None:
        resp = await client.post("/api/scrape/ghost/enqueue", json={})
        assert resp.status_code == 404

    async def test_enqueue_accepts_empty_body(
        self, client: AsyncClient, seeded_source
    ) -> None:
        with patch(
            "magpie.api.routers.jobs.scrape_source_task.defer_async",
            new=AsyncMock(return_value=99),
        ):
            resp = await client.post("/api/scrape/src/enqueue", json={})
        assert resp.status_code == 202


class TestGetRunEndpoint:
    async def test_get_run_returns_status(
        self, client: AsyncClient, session_factory, seeded_source
    ) -> None:
        async with session_factory() as session:
            src = await SourcesRepository(session).get_by_name("src")
            assert src is not None
            repo = PgRunRepository(session)
            run = await repo.create_queued(
                source_id=src.id, source_name=src.name, job_id="abc"
            )
            await session.commit()
            run_id = run.id

        resp = await client.get(f"/api/runs/{run_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        assert body["source"] == "src"
        assert body["job_id"] == "abc"

    async def test_get_run_missing_404(self, client: AsyncClient) -> None:
        resp = await client.get(f"/api/runs/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_get_run_invalid_uuid_422(self, client: AsyncClient) -> None:
        resp = await client.get("/api/runs/not-a-uuid")
        assert resp.status_code == 422
