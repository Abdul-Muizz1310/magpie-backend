"""Postgres integration tier — spec 08, cases 1-10.

Every assertion here is one SQLite cannot make: enforced foreign keys, real
``SELECT … FOR UPDATE`` row locks, native enum domains, and the Alembic chain
applied to the engine production actually runs on.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
import yaml
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from alembic import command
from magpie.api.deps import get_db_session, get_session_factory_dep
from magpie.config.schema import SourceConfig
from magpie.main import app
from magpie.storage import db as db_module
from magpie.storage.heals_repo import HealsRepository
from magpie.storage.items_repo_pg import PgItemRepository
from magpie.storage.models import Heal, HealMode, Item, Run, Source, SourceOrigin
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository

from .conftest import PgUrls

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]

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
health:
  min_items: 20
"""


async def _seed_source(
    factory: async_sessionmaker[AsyncSession], *, name: str = "hackernews"
) -> uuid.UUID:
    cfg = SourceConfig(**{**yaml.safe_load(SEED_YAML), "name": name})
    async with factory() as session:
        src = await SourcesRepository(session).create(
            config=cfg, origin=SourceOrigin.file, yaml_text=SEED_YAML
        )
        await session.commit()
        return src.id


# ── Case 1: the migration chain on Postgres ──────────────────────────────────


class TestAlembicOnPostgres:
    def _alembic_config(self) -> Config:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        return cfg

    def _run(self, target: str, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", url)
        cwd = os.getcwd()
        os.chdir(REPO_ROOT)
        try:
            if target == "base":
                command.downgrade(self._alembic_config(), "base")
            else:
                command.upgrade(self._alembic_config(), target)
        finally:
            os.chdir(cwd)

    def test_upgrade_head_builds_the_full_postgres_schema(
        self, pg_database: PgUrls, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy import create_engine

        self._run("head", pg_database.sync_url, monkeypatch)

        engine = create_engine(pg_database.sync_url, poolclass=NullPool)
        try:
            with engine.connect() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                }
                assert {"sources", "runs", "items", "heals"} <= tables

                # Native enum types — on SQLite these degrade to VARCHAR and
                # pg_type has nothing to show.
                enums = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT t.typname FROM pg_type t "
                            "JOIN pg_enum e ON e.enumtypid = t.oid GROUP BY t.typname"
                        )
                    )
                }
                assert {"source_origin", "run_status", "heal_mode"} <= enums

                indexes = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE tablename = 'items'")
                    )
                }
                assert "ix_items_source_removed_last_seen" in indexes
                assert "ix_items_source_removed" in indexes
        finally:
            engine.dispose()

    def test_downgrade_base_removes_the_tables(
        self, pg_database: PgUrls, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy import create_engine

        self._run("head", pg_database.sync_url, monkeypatch)
        self._run("base", pg_database.sync_url, monkeypatch)

        engine = create_engine(pg_database.sync_url, poolclass=NullPool)
        try:
            with engine.connect() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                }
            assert not ({"sources", "runs", "items", "heals"} & tables)
        finally:
            engine.dispose()


# ── Case 2: URL normalisation reaches a real asyncpg connection ──────────────


class TestDatabaseUrlNormalisation:
    async def test_stock_postgresql_url_connects_through_normalize(
        self, pg_database: PgUrls, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare ``postgresql://`` URL — what Neon and Render emit — must work."""
        assert pg_database.plain_url.startswith("postgresql://")
        monkeypatch.setenv("DATABASE_URL", pg_database.plain_url)
        await db_module.reset_engine()
        try:
            assert db_module._database_url().startswith("postgresql+asyncpg://")
            assert await db_module.check_db() is True
        finally:
            await db_module.reset_engine()

    async def test_sslmode_query_param_is_translated_for_asyncpg(
        self, pg_database: PgUrls, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``sslmode=`` is libpq's spelling; asyncpg only accepts ``ssl=``.

        Passing it through unrenamed raises ``TypeError: connect() got an
        unexpected keyword argument 'sslmode'`` — the exact Neon failure the
        rewrite exists for.
        """
        monkeypatch.setenv("DATABASE_URL", f"{pg_database.plain_url}?sslmode=prefer")
        await db_module.reset_engine()
        try:
            assert "ssl=prefer" in db_module._database_url()
            assert await db_module.check_db() is True
        finally:
            await db_module.reset_engine()

    async def test_unreachable_postgres_reports_false_not_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:nobody@127.0.0.1:1/does-not-exist")
        await db_module.reset_engine()
        try:
            assert await db_module.check_db() is False
        finally:
            await db_module.reset_engine()


# ── Cases 3-6: constraints the database itself must enforce ─────────────────


class TestPostgresEnforcedConstraints:
    async def test_unique_source_dedupe_key_is_enforced_by_the_database(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        source_id = await _seed_source(pg_session_factory)
        async with pg_session_factory() as session:
            session.add(
                Item(source_id=source_id, dedupe_key="k1", content_hash="h1", data={"v": 1})
            )
            await session.commit()
        async with pg_session_factory() as session:
            session.add(
                Item(source_id=source_id, dedupe_key="k1", content_hash="h2", data={"v": 2})
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_deleting_a_source_cascades_to_items_runs_and_heals(
        self, pg_session_factory: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine
    ) -> None:
        """SQLite skips FK enforcement entirely without ``PRAGMA foreign_keys=ON``."""
        source_id = await _seed_source(pg_session_factory)
        async with pg_session_factory() as session:
            run = await PgRunRepository(session).create_queued(
                source_id=source_id, source_name="hackernews"
            )
            session.add(Item(source_id=source_id, dedupe_key="k", content_hash="h", data={"v": 1}))
            await HealsRepository(session).create(
                source_id=source_id,
                run_id=run.id,
                field_name="title",
                old_selector="a::text",
                new_selector="b::text",
                selector_type="css",
                confidence=0.5,
                reasoning="drift",
                sample_values=["x"],
                mode=HealMode.pr,
                pr_url=None,
                applied=False,
            )
            await session.commit()

        # Raw DELETE so the ORM's own cascade can't be what we end up testing.
        async with pg_engine.begin() as conn:
            await conn.execute(text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id})

        async with pg_session_factory() as session:
            for model in (Item, Run, Heal):
                remaining = await session.execute(select(func.count()).select_from(model))
                assert remaining.scalar_one() == 0, f"{model.__name__} rows survived the cascade"

    async def test_deleting_a_run_nulls_the_heal_reference(
        self, pg_session_factory: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine
    ) -> None:
        source_id = await _seed_source(pg_session_factory)
        async with pg_session_factory() as session:
            run = await PgRunRepository(session).create_queued(
                source_id=source_id, source_name="hackernews"
            )
            await HealsRepository(session).create(
                source_id=source_id,
                run_id=run.id,
                field_name="title",
                old_selector="a::text",
                new_selector="b::text",
                selector_type="css",
                confidence=0.5,
                reasoning="drift",
                sample_values=["x"],
                mode=HealMode.pr,
                pr_url=None,
                applied=False,
            )
            await session.commit()
            run_id = run.id

        async with pg_engine.begin() as conn:
            await conn.execute(text("DELETE FROM runs WHERE id = :rid"), {"rid": run_id})

        async with pg_session_factory() as session:
            heal = (await session.execute(select(Heal))).scalar_one()
            assert heal.run_id is None
            assert heal.source_id == source_id

    async def test_native_enum_rejects_an_out_of_domain_status(
        self, pg_session_factory: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine
    ) -> None:
        source_id = await _seed_source(pg_session_factory)
        with pytest.raises(DBAPIError):
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO runs "
                        "(id, source_id, source_name, status, started_at, duration_ms, "
                        " item_count, items_new, items_updated, items_removed, created_at) "
                        "VALUES (:id, :sid, 'hackernews', 'bogus', now(), 0, 0, 0, 0, 0, now())"
                    ),
                    {"id": uuid.uuid4(), "sid": source_id},
                )


# ── Cases 7-8: the repository contract on real Postgres ─────────────────────


class TestPgItemRepositoryOnPostgres:
    async def test_full_dedup_lifecycle(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        source_id = await _seed_source(pg_session_factory)

        async with pg_session_factory() as session:
            repo = PgItemRepository(session)
            first = await repo.persist_items(
                source_id,
                [{"id": "1", "title": "a"}, {"id": "2", "title": "b"}],
                dedupe_key="id",
            )
            await session.commit()
        assert (first.items_new, first.items_updated, first.items_removed) == (2, 0, 0)

        async with pg_session_factory() as session:
            updated = await PgItemRepository(session).persist_items(
                source_id,
                [{"id": "1", "title": "a-changed"}],
                dedupe_key="id",
            )
            await session.commit()
        # "2" vanished from the page → soft-removed; "1" changed → updated.
        assert (updated.items_new, updated.items_updated, updated.items_removed) == (0, 1, 1)

        async with pg_session_factory() as session:
            reappeared = await PgItemRepository(session).persist_items(
                source_id,
                [{"id": "1", "title": "a-changed"}, {"id": "2", "title": "b"}],
                dedupe_key="id",
            )
            await session.commit()
        assert (reappeared.items_new, reappeared.items_removed) == (1, 0)

        async with pg_session_factory() as session:
            live = await PgItemRepository(session).list_for_source(source_id=source_id)
        assert {row.dedupe_key for row in live} == {"1", "2"}

    async def test_concurrent_persists_of_one_source_are_serialised(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Overlapping batches must not collide on ``uq_items_source_dedupe``.

        ``persist_items`` takes ``SELECT sources.id … FOR UPDATE`` before reading
        the existing rows. Postgres honours it, so the second transaction blocks
        until the first commits and then *sees* the shared key. Without the lock
        both would read an empty table and both would try to insert ``"b"`` — one
        of them raising ``IntegrityError``. SQLite ignores ``FOR UPDATE``, so this
        assertion is only meaningful here.

        Note what the lock does *not* promise: ``persist_items`` treats its batch
        as a whole-page snapshot, so whichever transaction commits second
        soft-removes the keys the other one added but it didn't see. That is the
        documented dedup contract (spec 02), not a locking failure — hence the
        assertions below are about *row identity* and the insert count, not about
        which keys are still live.
        """
        source_id = await _seed_source(pg_session_factory)
        shared = {"id": "b", "title": "shared"}

        async def persist(batch: list[dict[str, str]]) -> int:
            async with pg_session_factory() as session:
                result = await PgItemRepository(session).persist_items(
                    source_id, batch, dedupe_key="id"
                )
                await session.commit()
                return result.items_new

        new_counts = await asyncio.gather(
            persist([{"id": "a", "title": "first"}, dict(shared)]),
            persist([dict(shared), {"id": "c", "title": "third"}]),
        )

        # Union of the two batches is {a, b, c}: 3 inserts, never 4. A 4th would
        # mean the second transaction never observed the first's "b".
        assert sum(new_counts) == 3
        async with pg_session_factory() as session:
            rows = (
                (await session.execute(select(Item).where(Item.source_id == source_id)))
                .scalars()
                .all()
            )
        # Exactly one physical row per dedupe_key — no duplicate slipped past the
        # unique index, and nothing was lost.
        assert sorted(row.dedupe_key for row in rows) == ["a", "b", "c"]


# ── Case 9: the reaper against timestamptz ──────────────────────────────────


class TestStaleRunReaperOnPostgres:
    async def test_reaps_only_runs_older_than_the_cutoff(
        self, pg_session_factory: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine
    ) -> None:
        source_id = await _seed_source(pg_session_factory)
        async with pg_session_factory() as session:
            repo = PgRunRepository(session)
            stale = await repo.create_queued(source_id=source_id, source_name="hackernews")
            fresh = await repo.create_queued(source_id=source_id, source_name="hackernews")
            await repo.mark_running(stale.id)
            await repo.mark_running(fresh.id)
            await session.commit()
            stale_id, fresh_id = stale.id, fresh.id

        # Backdate one run in the database so the comparison is Postgres'
        # timestamptz arithmetic, not Python's.
        async with pg_engine.begin() as conn:
            await conn.execute(
                text("UPDATE runs SET started_at = now() - interval '2 hours' WHERE id = :rid"),
                {"rid": stale_id},
            )

        async with pg_session_factory() as session:
            reaped = await PgRunRepository(session).mark_stale_running_as_error(
                older_than_seconds=1800
            )
            await session.commit()
        assert reaped == 1

        async with pg_session_factory() as session:
            repo = PgRunRepository(session)
            stale_row = await repo.get(stale_id)
            fresh_row = await repo.get(fresh_id)
        assert stale_row is not None and stale_row.status.value == "error"
        assert stale_row.error is not None and "Stale run reaped" in stale_row.error
        assert fresh_row is not None and fresh_row.status.value == "running"


# ── Case 10: the viewer API on Postgres ─────────────────────────────────────


class TestViewerApiOnPostgres:
    @pytest.fixture
    async def client(self, pg_session_factory: async_sessionmaker[AsyncSession]) -> AsyncClient:
        async def _factory_override() -> async_sessionmaker[AsyncSession]:
            return pg_session_factory

        async def _session_override() -> AsyncSession:
            async with pg_session_factory() as session:
                yield session

        app.dependency_overrides[get_session_factory_dep] = _factory_override
        app.dependency_overrides[get_db_session] = _session_override
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                yield c
        finally:
            app.dependency_overrides.clear()

    async def test_end_to_end_viewer_surface(
        self, client: AsyncClient, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        source_id = await _seed_source(pg_session_factory)
        async with pg_session_factory() as session:
            runs = PgRunRepository(session)
            run = await runs.create_queued(source_id=source_id, source_name="hackernews")
            await runs.mark_ok(run.id, item_count=2, items_new=2, items_updated=0, items_removed=0)
            await PgItemRepository(session).persist_items(
                source_id,
                [{"id": "1", "title": "first", "url": "/item?id=1"}, {"id": "2", "title": "2nd"}],
                dedupe_key="id",
            )
            await HealsRepository(session).create(
                source_id=source_id,
                run_id=run.id,
                field_name="title",
                old_selector="a.old::text",
                new_selector="a.new::text",
                selector_type="css",
                confidence=0.9,
                reasoning="selector drift",
                sample_values=["x"],
                mode=HealMode.pr,
                pr_url="https://github.com/owner/repo/pull/1",
                applied=False,
            )
            await session.commit()

        sources = await client.get("/sources")
        assert sources.status_code == 200
        assert sources.json()[0]["name"] == "hackernews"
        assert sources.json()[0]["last_status"] == "ok"
        assert sources.json()[0]["item_count"] == 2

        runs_resp = await client.get("/runs", params={"source": "hackernews"})
        assert runs_resp.status_code == 200
        assert runs_resp.json()[0]["items_new"] == 2

        heals_resp = await client.get("/heals", params={"source": "hackernews"})
        assert heals_resp.status_code == 200
        assert heals_resp.json()[0]["pr_url"] == "https://github.com/owner/repo/pull/1"

        items_resp = await client.get("/sources/hackernews/items")
        assert items_resp.status_code == 200
        body = items_resp.json()
        assert {row["title"] for row in body} == {"first", "2nd"}
        first = next(row for row in body if row["title"] == "first")
        # Relative scraped URL resolved against the source's own base URL.
        assert first["url"] == "https://news.ycombinator.com/item?id=1"

    async def test_unknown_source_is_404_on_postgres(self, client: AsyncClient) -> None:
        assert (await client.get("/sources/ghost")).status_code == 404


# ── Spec 08 edge case: per-test database isolation ──────────────────────────


class TestPerTestDatabaseIsolation:
    """Each test gets its own ``CREATE DATABASE``-d database on the shared container.

    These two run in declaration order against the same session-scoped container.
    If the fixture ever handed out a shared database, the second would see the
    first's row and fail — which is the point: ordering must not be able to leak
    state between tests in this tier.
    """

    async def _source_count(self, factory: async_sessionmaker[AsyncSession]) -> int:
        async with factory() as session:
            return int(
                (await session.execute(select(func.count()).select_from(Source))).scalar_one()
            )

    async def test_first_writer_leaves_exactly_one_row(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await self._source_count(pg_session_factory) == 0
        await _seed_source(pg_session_factory, name="isolation-probe")
        assert await self._source_count(pg_session_factory) == 1

    async def test_second_writer_starts_from_an_empty_database(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await self._source_count(pg_session_factory) == 0, (
            "state leaked in from another test's database"
        )
        # Same name as the previous test — a shared database would collide on
        # the unique sources.name index.
        await _seed_source(pg_session_factory, name="isolation-probe")
        assert await self._source_count(pg_session_factory) == 1
