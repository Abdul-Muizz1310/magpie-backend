"""Postgres-backed run repository.

Exposes the three operations the service layer, async task, and API need:
create a queued row at enqueue time, transition its state as the worker picks
it up, and query rows back for the viewer endpoint. The synchronous in-memory
``RunRepository`` stays around for unit tests that don't need a DB.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from magpie.storage.models import Run, RunStatus


class RunNotFoundError(Exception):
    def __init__(self, run_id: uuid.UUID | str) -> None:
        super().__init__(f"Run {run_id} not found")
        self.run_id = run_id


class PgRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_queued(
        self,
        *,
        source_id: uuid.UUID,
        source_name: str,
        job_id: str | None = None,
    ) -> Run:
        now = datetime.now(UTC)
        run = Run(
            source_id=source_id,
            source_name=source_name,
            status=RunStatus.queued,
            started_at=now,
            job_id=job_id,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def mark_running(self, run_id: uuid.UUID) -> Run:
        run = await self._get(run_id)
        run.status = RunStatus.running
        run.started_at = datetime.now(UTC)
        await self._session.flush()
        return run

    async def mark_ok(
        self,
        run_id: uuid.UUID,
        *,
        item_count: int,
        items_new: int,
        items_updated: int,
        items_removed: int,
        started_at: datetime | None = None,
    ) -> Run:
        run = await self._get(run_id)
        ended = datetime.now(UTC)
        actual_start = started_at or run.started_at
        run.status = RunStatus.ok
        run.ended_at = ended
        run.duration_ms = max(0, int((ended - actual_start).total_seconds() * 1000))
        run.item_count = item_count
        run.items_new = items_new
        run.items_updated = items_updated
        run.items_removed = items_removed
        run.error = None
        await self._session.flush()
        return run

    async def mark_error(
        self,
        run_id: uuid.UUID,
        *,
        error: str,
        started_at: datetime | None = None,
    ) -> Run:
        run = await self._get(run_id)
        ended = datetime.now(UTC)
        actual_start = started_at or run.started_at
        run.status = RunStatus.error
        run.ended_at = ended
        run.duration_ms = max(0, int((ended - actual_start).total_seconds() * 1000))
        run.item_count = 0
        run.error = error
        await self._session.flush()
        return run

    async def get(self, run_id: uuid.UUID) -> Run | None:
        return await self._session.get(Run, run_id)

    async def list_runs(
        self,
        *,
        source_name: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[Run]:
        stmt = select(Run).order_by(desc(Run.started_at)).limit(limit).offset(offset)
        if source_name is not None:
            stmt = stmt.where(Run.source_name == source_name)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def latest_failed_runs(self) -> Sequence[Run]:
        stmt = select(Run).where(Run.status == RunStatus.error).order_by(desc(Run.started_at))
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def mark_stale_running_as_error(self, *, older_than_seconds: int) -> int:
        """Transition ``running`` rows older than ``older_than_seconds`` to error.

        Used by the periodic reaper. Returns the number of rows touched so the
        caller can log it. A worker crashing or the free-tier web instance
        sleeping mid-scrape is exactly the kind of thing this catches.
        """
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        result = await self._session.execute(
            select(Run).where(Run.status == RunStatus.running).where(Run.started_at < cutoff)
        )
        stale = list(result.scalars().all())
        for run in stale:
            ended = datetime.now(UTC)
            run.status = RunStatus.error
            run.ended_at = ended
            run.duration_ms = max(0, int((ended - run.started_at).total_seconds() * 1000))
            run.error = (
                f"Stale run reaped after {older_than_seconds}s in 'running' — assumed worker crash"
            )
        await self._session.flush()
        return len(stale)

    async def _get(self, run_id: uuid.UUID) -> Run:
        run = await self._session.get(Run, run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run
