"""Async-scraping endpoints backed by Procrastinate.

``POST /api/scrape/{source}/enqueue`` creates a queued ``runs`` row and defers
the Procrastinate task carrying its id; ``GET /api/runs/{run_id}`` lets the
caller poll progress. Synchronous scraping endpoints still live in the
``scrape`` router — these are the long-running path.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from magpie.api.deps import get_db_session, get_session_factory_dep
from magpie.queue.tasks import scrape_source_task
from magpie.schemas.jobs import EnqueueResponse, RunView
from magpie.schemas.scrape import ScrapeOnceRequest
from magpie.storage.models import Run
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository

router = APIRouter(tags=["jobs"])

_Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory_dep)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]


@router.post("/api/scrape/{source}/enqueue", response_model=EnqueueResponse, status_code=202)
async def enqueue_scrape(
    source: str,
    factory: _Factory,
    body: ScrapeOnceRequest | None = None,
) -> EnqueueResponse:
    """Defer a scrape task; return immediately with the run id to poll."""
    request = body or ScrapeOnceRequest()

    async with factory() as session:
        sources = SourcesRepository(session)
        src = await sources.get_by_name(source)
        if src is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown source: {source}",
            )
        runs = PgRunRepository(session)
        run = await runs.create_queued(source_id=src.id, source_name=src.name)
        await session.commit()
        run_id = run.id

    job_id = await scrape_source_task.defer_async(
        source=source,
        max_items=request.max_items,
        run_id=str(run_id),
    )

    # Record the job_id for visibility once the deferral has a handle.
    async with factory() as session:
        persisted_run = await session.get(Run, run_id)
        if persisted_run is not None:
            persisted_run.job_id = str(job_id) if job_id is not None else None
            await session.commit()

    return EnqueueResponse(
        run_id=run_id,
        job_id=str(job_id) if job_id is not None else None,
        source=source,
        status="queued",
    )


@router.get("/api/runs/{run_id}", response_model=RunView)
async def get_run(run_id: uuid.UUID, session: _Session) -> RunView:
    repo = PgRunRepository(session)
    row = await repo.get(run_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )
    return RunView(
        id=row.id,
        source=row.source_name,
        status=row.status.value,
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_ms=row.duration_ms,
        item_count=row.item_count,
        items_new=row.items_new,
        items_updated=row.items_updated,
        items_removed=row.items_removed,
        error=row.error,
        job_id=row.job_id,
    )


