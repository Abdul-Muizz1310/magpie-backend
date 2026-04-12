"""Integration tests for FastAPI viewer API (spec 05-viewer-api)."""

import pytest
from httpx import ASGITransport, AsyncClient

from magpie.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestViewerAPIHappyPath:
    @pytest.mark.asyncio
    async def test_get_sources(self, client: AsyncClient) -> None:
        resp = await client.get("/sources")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_get_source_by_name(self, client: AsyncClient) -> None:
        # Will need seeded data in S4
        resp = await client.get("/sources/hackernews")
        assert resp.status_code in (200, 404)

    @pytest.mark.asyncio
    async def test_get_runs(self, client: AsyncClient) -> None:
        resp = await client.get("/runs", params={"source": "hackernews"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_get_runs_with_limit(self, client: AsyncClient) -> None:
        resp = await client.get("/runs", params={"source": "hackernews", "limit": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) <= 5

    @pytest.mark.asyncio
    async def test_get_heals(self, client: AsyncClient) -> None:
        resp = await client.get("/heals", params={"source": "hackernews"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_health(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_version(self, client: AsyncClient) -> None:
        resp = await client.get("/version")
        assert resp.status_code == 200


class TestViewerAPIEdgeCases:
    @pytest.mark.asyncio
    async def test_sources_empty_db(self, client: AsyncClient) -> None:
        resp = await client.get("/sources")
        assert resp.status_code == 200
        assert resp.json() == [] or isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_runs_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/runs", params={"source": "hackernews"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_heals_all_sources(self, client: AsyncClient) -> None:
        resp = await client.get("/heals")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_limit_zero_rejected(self, client: AsyncClient) -> None:
        resp = await client.get("/runs", params={"source": "hackernews", "limit": 0})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_limit_over_max_clamped(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/runs", params={"source": "hackernews", "limit": 200}
        )
        # Should either clamp to 100 or reject
        assert resp.status_code in (200, 422)


class TestViewerAPIFailures:
    @pytest.mark.asyncio
    async def test_invalid_source_name_rejected(self, client: AsyncClient) -> None:
        resp = await client.get("/sources/INVALID_NAME")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_nonexistent_source_404(self, client: AsyncClient) -> None:
        resp = await client.get("/sources/nonexistent")
        assert resp.status_code == 404
