"""Tests for PgRunRepository state transitions and listing."""

from __future__ import annotations

import pytest

from magpie.storage.models import RunStatus, Source, SourceOrigin
from magpie.storage.runs_repo_pg import PgRunRepository, RunNotFoundError


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


class TestPgRunRepository:
    async def test_create_queued_defaults(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgRunRepository(db_session)
        run = await repo.create_queued(
            source_id=src.id, source_name=src.name, job_id="job-1"
        )
        assert run.status is RunStatus.queued
        assert run.job_id == "job-1"
        assert run.item_count == 0

    async def test_transition_running_then_ok(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgRunRepository(db_session)
        run = await repo.create_queued(source_id=src.id, source_name=src.name)
        await repo.mark_running(run.id)
        ok = await repo.mark_ok(
            run.id,
            item_count=5,
            items_new=5,
            items_updated=0,
            items_removed=0,
        )
        assert ok.status is RunStatus.ok
        assert ok.item_count == 5
        assert ok.duration_ms >= 0

    async def test_transition_to_error(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgRunRepository(db_session)
        run = await repo.create_queued(source_id=src.id, source_name=src.name)
        errored = await repo.mark_error(run.id, error="boom")
        assert errored.status is RunStatus.error
        assert errored.error == "boom"

    async def test_mark_missing_run_raises(self, db_session) -> None:
        import uuid as _uuid

        repo = PgRunRepository(db_session)
        with pytest.raises(RunNotFoundError):
            await repo.mark_running(_uuid.uuid4())

    async def test_list_runs_filters_and_orders(self, db_session) -> None:
        src_a = await _make_source(db_session, "src-a")
        src_b = await _make_source(db_session, "src-b")
        repo = PgRunRepository(db_session)
        await repo.create_queued(source_id=src_a.id, source_name=src_a.name)
        await repo.create_queued(source_id=src_b.id, source_name=src_b.name)
        await repo.create_queued(source_id=src_a.id, source_name=src_a.name)

        for_a = await repo.list_runs(source_name="src-a")
        assert len(for_a) == 2
        assert all(r.source_name == "src-a" for r in for_a)

        all_runs = await repo.list_runs(limit=10)
        assert len(all_runs) == 3

    async def test_latest_failed_runs(self, db_session) -> None:
        src = await _make_source(db_session)
        repo = PgRunRepository(db_session)
        r1 = await repo.create_queued(source_id=src.id, source_name=src.name)
        await repo.mark_ok(r1.id, item_count=1, items_new=1, items_updated=0, items_removed=0)
        r2 = await repo.create_queued(source_id=src.id, source_name=src.name)
        await repo.mark_error(r2.id, error="oops")

        failed = await repo.latest_failed_runs()
        assert len(failed) == 1
        assert failed[0].id == r2.id
