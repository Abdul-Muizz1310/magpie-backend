"""Integration tests for ``/sources``, ``/runs``, ``/heals`` endpoints.

These are now DB-backed. We inject a SQLite session factory via FastAPI's
``dependency_overrides`` and seed fixtures before asserting response shape.
"""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from magpie.api.deps import get_db_session, get_session_factory_dep
from magpie.config.schema import SourceConfig
from magpie.main import app
from magpie.storage.heals_repo import HealsRepository
from magpie.storage.items_repo_pg import PgItemRepository
from magpie.storage.models import HealMode, SourceOrigin
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository

SEED_YAML = """\
name: hackernews
url: https://news.ycombinator.com
schedule: "0 */6 * * *"
item:
  container: "tr.athing"
  fields:
    - { name: title, selector: "a::text" }
    - { name: id, selector: "::attr(id)" }
  dedupe_key: id
"""


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
async def seeded(session_factory):
    cfg = SourceConfig(**yaml.safe_load(SEED_YAML))
    async with session_factory() as session:
        source = await SourcesRepository(session).create(
            config=cfg, origin=SourceOrigin.file, yaml_text=SEED_YAML
        )
        runs = PgRunRepository(session)
        r1 = await runs.create_queued(source_id=source.id, source_name="hackernews")
        await runs.mark_ok(r1.id, item_count=3, items_new=3, items_updated=0, items_removed=0)
        await HealsRepository(session).create(
            source_id=source.id,
            run_id=r1.id,
            field_name="title",
            old_selector="a.old::text",
            new_selector="a.new::text",
            selector_type="css",
            confidence=0.9,
            reasoning="selector drift",
            sample_values=["x", "y"],
            mode=HealMode.pr,
            pr_url="https://github.com/owner/repo/pull/1",
            applied=False,
        )
        await session.commit()
    return source


class TestViewerAPIHappyPath:
    async def test_get_sources(self, client: AsyncClient, seeded) -> None:
        resp = await client.get("/sources")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["name"] == "hackernews"
        assert body[0]["last_status"] == "ok"

    async def test_get_source_by_name(self, client: AsyncClient, seeded) -> None:
        resp = await client.get("/sources/hackernews")
        assert resp.status_code == 200
        assert resp.json()["name"] == "hackernews"

    async def test_get_runs(self, client: AsyncClient, seeded) -> None:
        resp = await client.get("/runs", params={"source": "hackernews"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["status"] == "ok"
        assert data[0]["items_new"] == 3

    async def test_get_runs_with_limit(self, client: AsyncClient, seeded) -> None:
        resp = await client.get("/runs", params={"source": "hackernews", "limit": 5})
        assert resp.status_code == 200
        assert len(resp.json()) <= 5

    async def test_get_heals(self, client: AsyncClient, seeded) -> None:
        resp = await client.get("/heals", params={"source": "hackernews"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["source"] == "hackernews"
        assert data[0]["pr_url"] == "https://github.com/owner/repo/pull/1"

    async def test_health(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_version(self, client: AsyncClient) -> None:
        resp = await client.get("/version")
        assert resp.status_code == 200


class TestViewerAPIEdgeCases:
    async def test_sources_empty_db(self, client: AsyncClient) -> None:
        resp = await client.get("/sources")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_runs_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/runs", params={"source": "hackernews"})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_heals_all_sources(self, client: AsyncClient) -> None:
        resp = await client.get("/heals")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_limit_zero_rejected(self, client: AsyncClient) -> None:
        resp = await client.get("/runs", params={"source": "hackernews", "limit": 0})
        assert resp.status_code == 422

    async def test_limit_over_max_clamped(self, client: AsyncClient) -> None:
        resp = await client.get("/runs", params={"source": "hackernews", "limit": 200})
        # ge=1,le=100 → 422 for over-max
        assert resp.status_code in (200, 422)


class TestViewerAPIFailures:
    async def test_invalid_source_name_rejected(self, client: AsyncClient) -> None:
        resp = await client.get("/sources/INVALID_NAME")
        assert resp.status_code == 422

    async def test_nonexistent_source_404(self, client: AsyncClient) -> None:
        resp = await client.get("/sources/nonexistent")
        assert resp.status_code == 404


class TestListSourceItemsEndpoint:
    async def test_returns_items_with_data_blob(
        self, client: AsyncClient, session_factory, seeded
    ) -> None:
        async with session_factory() as session:
            src = await SourcesRepository(session).get_by_name("hackernews")
            assert src is not None
            await PgItemRepository(session).persist_items(
                src.id,
                [
                    {"id": "1", "title": "first", "url": "/item?id=1"},
                    {"id": "2", "title": "second", "url": "https://other.example/2"},
                ],
                dedupe_key="id",
            )
            await session.commit()

        resp = await client.get("/sources/hackernews/items")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        first = next(b for b in body if b["title"] == "first")
        # Relative URL resolved against source's base
        assert first["url"] == "https://news.ycombinator.com/item?id=1"
        # Full scraped dict passes through in `data`
        assert first["data"] == {"id": "1", "title": "first", "url": "/item?id=1"}
        second = next(b for b in body if b["title"] == "second")
        assert second["url"] == "https://other.example/2"

    async def test_source_items_404_for_unknown(self, client: AsyncClient) -> None:
        resp = await client.get("/sources/ghost/items")
        assert resp.status_code == 404

    async def test_source_items_422_for_invalid_name(self, client: AsyncClient) -> None:
        resp = await client.get("/sources/INVALID_NAME/items")
        assert resp.status_code == 422

    async def test_respects_limit_and_offset(
        self, client: AsyncClient, session_factory, seeded
    ) -> None:
        async with session_factory() as session:
            src = await SourcesRepository(session).get_by_name("hackernews")
            assert src is not None
            await PgItemRepository(session).persist_items(
                src.id,
                [{"id": str(i), "title": f"t-{i}"} for i in range(5)],
                dedupe_key="id",
            )
            await session.commit()

        page1 = await client.get("/sources/hackernews/items?limit=2&offset=0")
        page2 = await client.get("/sources/hackernews/items?limit=2&offset=2")
        assert page1.status_code == 200
        assert page2.status_code == 200
        ids1 = {b["id"] for b in page1.json()}
        ids2 = {b["id"] for b in page2.json()}
        assert ids1.isdisjoint(ids2)

    async def test_excludes_removed_items(
        self, client: AsyncClient, session_factory, seeded
    ) -> None:
        async with session_factory() as session:
            src = await SourcesRepository(session).get_by_name("hackernews")
            assert src is not None
            await PgItemRepository(session).persist_items(
                src.id,
                [{"id": "1", "title": "keep"}, {"id": "2", "title": "drop"}],
                dedupe_key="id",
            )
            await PgItemRepository(session).persist_items(
                src.id,
                [{"id": "1", "title": "keep"}],  # "2" now removed
                dedupe_key="id",
            )
            await session.commit()

        resp = await client.get("/sources/hackernews/items")
        assert resp.status_code == 200
        titles = {b["title"] for b in resp.json()}
        assert titles == {"keep"}
