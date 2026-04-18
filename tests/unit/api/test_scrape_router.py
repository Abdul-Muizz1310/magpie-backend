"""Router-level tests for POST /api/scrape/* (spec 06-batch-scrape).

Service layer is mocked — these tests assert request parsing, response
shape, and error-surface mapping only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from magpie.main import app
from magpie.schemas.scrape import ScrapeItem, ScrapeResult
from magpie.services.scrape_service import ScrapeExecutionError, UnknownSourceError


def _canned_result(source: str = "hackernews", n_items: int = 3) -> ScrapeResult:
    now = datetime.now(UTC)
    items = [
        ScrapeItem(
            stable_id=f"stable-{i}",
            url=f"https://example.com/{i}",
            title=f"Item {i}",
            content_text=f"body {i}",
            content_hash=f"hash-{i}" * 8,  # 64-ish chars
            fetched_at=now,
            html_snapshot_url=None,
        )
        for i in range(n_items)
    ]
    return ScrapeResult(
        source=source,
        scraped_at=now,
        run_id=uuid.uuid4(),
        items=tuple(items),
    )


@pytest.fixture
async def client() -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Pass cases ──────────────────────────────────────────────────────────────


class TestScrapeOnceHappyPath:
    @pytest.mark.asyncio
    async def test_valid_body_returns_200_with_shape(self, client: AsyncClient) -> None:
        fake = _canned_result(n_items=3)
        with patch(
            "magpie.api.routers.scrape.scrape_once",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            resp = await client.post("/api/scrape/hackernews/once", json={"max_items": 10})

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "hackernews"
        assert "scraped_at" in body
        assert "run_id" in body
        assert isinstance(body["items"], list)
        assert len(body["items"]) >= 1
        item0 = body["items"][0]
        for key in (
            "stable_id",
            "url",
            "title",
            "content_text",
            "content_hash",
            "fetched_at",
            "html_snapshot_url",
        ):
            assert key in item0

    @pytest.mark.asyncio
    async def test_max_items_respected(self, client: AsyncClient) -> None:
        fake = _canned_result(n_items=5)
        with patch(
            "magpie.api.routers.scrape.scrape_once",
            new_callable=AsyncMock,
            return_value=fake,
        ) as mock_scrape:
            resp = await client.post("/api/scrape/hackernews/once", json={"max_items": 5})

        assert resp.status_code == 200
        # Router must have passed max_items=5 through to the service
        kwargs = mock_scrape.call_args.kwargs
        assert kwargs["max_items"] == 5
        assert len(resp.json()["items"]) <= 5

    @pytest.mark.asyncio
    async def test_empty_body_applies_defaults(self, client: AsyncClient) -> None:
        fake = _canned_result(n_items=1)
        with patch(
            "magpie.api.routers.scrape.scrape_once",
            new_callable=AsyncMock,
            return_value=fake,
        ) as mock_scrape:
            resp = await client.post("/api/scrape/hackernews/once", json={})

        assert resp.status_code == 200
        kwargs = mock_scrape.call_args.kwargs
        assert kwargs["max_items"] == 10


class TestScrapeBatchHappyPath:
    @pytest.mark.asyncio
    async def test_two_valid_sources_returns_200_with_two_runs(self, client: AsyncClient) -> None:
        fake_results = [_canned_result("hackernews"), _canned_result("arxiv-cs")]
        with patch(
            "magpie.api.routers.scrape.scrape_batch",
            new_callable=AsyncMock,
            return_value=(fake_results, []),
        ):
            resp = await client.post(
                "/api/scrape/batch",
                json={"sources": ["hackernews", "arxiv-cs"], "max_items_per_source": 10},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["runs"]) == 2
        assert body["failed"] == []

    @pytest.mark.asyncio
    async def test_all_failing_sources_returns_empty_runs_full_failed(
        self, client: AsyncClient
    ) -> None:
        failed = [
            {"source": "hackernews", "error": "boom"},
            {"source": "arxiv-cs", "error": "timeout"},
        ]
        with patch(
            "magpie.api.routers.scrape.scrape_batch",
            new_callable=AsyncMock,
            return_value=([], failed),
        ):
            resp = await client.post(
                "/api/scrape/batch",
                json={"sources": ["hackernews", "arxiv-cs"]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["runs"] == []
        assert len(body["failed"]) == 2
        assert {f["source"] for f in body["failed"]} == {"hackernews", "arxiv-cs"}

    @pytest.mark.asyncio
    async def test_mixed_success_failure_returns_partial(self, client: AsyncClient) -> None:
        fake_results = [_canned_result("hackernews")]
        failed = [{"source": "arxiv-cs", "error": "offline"}]
        with patch(
            "magpie.api.routers.scrape.scrape_batch",
            new_callable=AsyncMock,
            return_value=(fake_results, failed),
        ):
            resp = await client.post(
                "/api/scrape/batch",
                json={"sources": ["hackernews", "arxiv-cs"]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["runs"]) == 1
        assert body["runs"][0]["source"] == "hackernews"
        assert len(body["failed"]) == 1
        assert body["failed"][0]["source"] == "arxiv-cs"


class TestScrapeResponseInvariants:
    @pytest.mark.asyncio
    async def test_scraped_at_is_timezone_aware_utc(self, client: AsyncClient) -> None:
        fake = _canned_result()
        with patch(
            "magpie.api.routers.scrape.scrape_once",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            resp = await client.post("/api/scrape/hackernews/once", json={})

        raw = resp.json()["scraped_at"]
        parsed = datetime.fromisoformat(raw)
        assert parsed.tzinfo is not None
        # UTC is represented by +00:00 offset
        assert parsed.utcoffset() is not None
        assert parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_run_id_is_valid_uuid(self, client: AsyncClient) -> None:
        fake = _canned_result()
        with patch(
            "magpie.api.routers.scrape.scrape_once",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            resp = await client.post("/api/scrape/hackernews/once", json={})

        run_id = resp.json()["run_id"]
        # Must parse as UUID
        parsed = uuid.UUID(run_id)
        assert str(parsed) == run_id


# ── Fail cases ──────────────────────────────────────────────────────────────


class TestScrapeOnceFailures:
    @pytest.mark.asyncio
    async def test_unknown_source_returns_404(self, client: AsyncClient) -> None:
        with patch(
            "magpie.api.routers.scrape.scrape_once",
            new_callable=AsyncMock,
            side_effect=UnknownSourceError("nonexistent"),
        ):
            resp = await client.post("/api/scrape/nonexistent/once", json={})

        assert resp.status_code == 404
        assert resp.json() == {"detail": "Unknown source: nonexistent"}

    @pytest.mark.asyncio
    async def test_max_items_zero_is_422(self, client: AsyncClient) -> None:
        resp = await client.post("/api/scrape/hackernews/once", json={"max_items": 0})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_max_items_over_cap_is_422(self, client: AsyncClient) -> None:
        resp = await client.post("/api/scrape/hackernews/once", json={"max_items": 101})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_source_slug_in_path_is_404(self, client: AsyncClient) -> None:
        # "Bad%20Name" is both slug-invalid AND not registered — expect 404.
        resp = await client.post("/api/scrape/Bad%20Name/once", json={})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_hard_scrape_failure_is_503(self, client: AsyncClient) -> None:
        with patch(
            "magpie.api.routers.scrape.scrape_once",
            new_callable=AsyncMock,
            side_effect=ScrapeExecutionError("network exploded"),
        ):
            resp = await client.post("/api/scrape/hackernews/once", json={})

        assert resp.status_code == 503


class TestScrapeBatchFailures:
    @pytest.mark.asyncio
    async def test_empty_sources_is_422(self, client: AsyncClient) -> None:
        resp = await client.post("/api/scrape/batch", json={"sources": []})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_over_ten_sources_is_422(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/scrape/batch",
            json={"sources": [f"src-{i}" for i in range(11)]},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_duplicate_sources_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/scrape/batch",
            json={"sources": ["hackernews", "hackernews"]},
        )
        assert resp.status_code in (400, 422)


# ── Security invariants ─────────────────────────────────────────────────────


class TestScrapeSecurityInvariants:
    @pytest.mark.asyncio
    async def test_unauthenticated_requests_not_blocked_here(self, client: AsyncClient) -> None:
        """Bastion gateway is the auth boundary; magpie does not gate this."""
        fake = _canned_result()
        with patch(
            "magpie.api.routers.scrape.scrape_once",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            # No Authorization header sent — must still work.
            resp = await client.post("/api/scrape/hackernews/once", json={})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_body_has_no_internal_db_ids(self, client: AsyncClient) -> None:
        fake = _canned_result()
        with patch(
            "magpie.api.routers.scrape.scrape_once",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            resp = await client.post("/api/scrape/hackernews/once", json={})

        body = resp.json()
        # Response must not leak surrogate ids — only stable_id + run_id allowed.
        forbidden_keys = {"id", "db_id", "_id", "row_id", "pk"}
        top_level_keys = set(body.keys())
        assert not (top_level_keys & forbidden_keys)
        for item in body["items"]:
            assert not (set(item.keys()) & forbidden_keys)
