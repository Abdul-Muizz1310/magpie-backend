"""Tests for HealsRepository."""

from __future__ import annotations

from magpie.storage.heals_repo import HealsRepository
from magpie.storage.models import HealMode, Source, SourceOrigin


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


class TestHealsRepository:
    async def test_create_and_list_for_source(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = HealsRepository(db_session)
        await repo.create(
            source_id=src.id,
            run_id=None,
            field_name="title",
            old_selector="a",
            new_selector="b",
            selector_type="css",
            confidence=0.9,
            reasoning="because",
            sample_values=["x"],
            mode=HealMode.db_patch,
            pr_url=None,
            applied=True,
        )
        heals = await repo.list_for_source(src.id)
        assert len(heals) == 1
        assert heals[0].mode is HealMode.db_patch
        assert heals[0].applied is True

    async def test_list_all_filters_by_source_name(self, db_session) -> None:
        src_a = await _make_source(db_session, "a")
        src_b = await _make_source(db_session, "b")
        repo = HealsRepository(db_session)
        for src in (src_a, src_b):
            await repo.create(
                source_id=src.id,
                run_id=None,
                field_name="title",
                old_selector="a",
                new_selector="b",
                selector_type="css",
                confidence=0.5,
                reasoning="x",
                sample_values=[],
                mode=HealMode.pr,
                pr_url="https://github.com/owner/repo/pull/1",
                applied=False,
            )
        only_a = await repo.list_all(source_name="a")
        assert len(only_a) == 1
        assert only_a[0].source_id == src_a.id

    async def test_list_all_returns_all_rows(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = HealsRepository(db_session)
        for i in range(3):
            await repo.create(
                source_id=src.id,
                run_id=None,
                field_name=f"f{i}",
                old_selector="a",
                new_selector="b",
                selector_type="css",
                confidence=0.5,
                reasoning="x",
                sample_values=[],
                mode=HealMode.pr,
                pr_url=None,
                applied=False,
            )
        all_heals = await repo.list_all()
        assert len(all_heals) == 3
        # We don't assert ordering beyond "most-recent-first by created_at" —
        # rows inserted in the same microsecond can tie, so assert the set.
        assert {h.field_name for h in all_heals} == {"f0", "f1", "f2"}
