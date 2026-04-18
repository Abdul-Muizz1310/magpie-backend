"""Tests for PgItemRepository — ported from the in-memory repo's test suite."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from magpie.storage.items_repo_pg import PgItemRepository
from magpie.storage.models import Item, Source, SourceOrigin


async def _make_source(session, name: str = "src") -> Source:
    src = Source(
        name=name,
        origin=SourceOrigin.api,
        config_yaml="name: src",
        config_sha="abc",
    )
    session.add(src)
    await session.flush()
    return src


class TestPgItemRepository:
    async def test_new_items_counted(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgItemRepository(db_session)
        result = await repo.persist_items(
            src.id,
            [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}],
            dedupe_key="id",
        )
        assert result.items_new == 2
        assert result.items_updated == 0
        assert result.items_removed == 0

    async def test_updated_items_counted(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgItemRepository(db_session)
        await repo.persist_items(
            src.id, [{"id": "1", "title": "A"}], dedupe_key="id"
        )
        result = await repo.persist_items(
            src.id, [{"id": "1", "title": "A updated"}], dedupe_key="id"
        )
        assert result.items_new == 0
        assert result.items_updated == 1
        assert result.items_removed == 0

    async def test_removed_items_marked_and_counted(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgItemRepository(db_session)
        await repo.persist_items(
            src.id,
            [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}],
            dedupe_key="id",
        )
        result = await repo.persist_items(
            src.id, [{"id": "1", "title": "A"}], dedupe_key="id"
        )
        assert result.items_removed == 1
        rows = (await db_session.execute(select(Item).where(Item.source_id == src.id))).scalars().all()
        removed = {row.dedupe_key: row.removed for row in rows}
        assert removed["2"] is True

    async def test_reappearing_item_counted_as_new(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgItemRepository(db_session)
        await repo.persist_items(
            src.id, [{"id": "1", "title": "A"}], dedupe_key="id"
        )
        await repo.persist_items(src.id, [], dedupe_key="id")  # mark 1 removed
        result = await repo.persist_items(
            src.id, [{"id": "1", "title": "A"}], dedupe_key="id"
        )
        assert result.items_new == 1

    async def test_unchanged_items_not_counted(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgItemRepository(db_session)
        await repo.persist_items(
            src.id, [{"id": "1", "title": "A"}], dedupe_key="id"
        )
        result = await repo.persist_items(
            src.id, [{"id": "1", "title": "A"}], dedupe_key="id"
        )
        assert result.items_new == 0
        assert result.items_updated == 0
        assert result.items_removed == 0

    async def test_missing_dedupe_key_raises(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgItemRepository(db_session)
        with pytest.raises(ValueError, match="missing dedupe_key"):
            await repo.persist_items(src.id, [{"title": "no id"}], dedupe_key="id")

    async def test_duplicate_dedupe_keys_in_batch_raises(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgItemRepository(db_session)
        with pytest.raises(ValueError, match="Duplicate dedupe_keys"):
            await repo.persist_items(
                src.id,
                [{"id": "1", "title": "A"}, {"id": "1", "title": "B"}],
                dedupe_key="id",
            )

    async def test_separate_sources_do_not_interact(self, db_session) -> None:
        src_a = await _make_source(db_session, "a")
        src_b = await _make_source(db_session, "b")
        repo = PgItemRepository(db_session)
        await repo.persist_items(src_a.id, [{"id": "1", "title": "A"}], dedupe_key="id")
        result_b = await repo.persist_items(
            src_b.id, [{"id": "1", "title": "B"}], dedupe_key="id"
        )
        assert result_b.items_new == 1
        assert result_b.items_removed == 0
