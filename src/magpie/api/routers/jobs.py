"""Async-scraping endpoints backed by Procrastinate.

``POST /api/scrape/{source}/enqueue`` creates a queued ``runs`` row and defers
the Procrastinate task carrying its id; ``GET /api/runs/{run_id}`` lets the
caller poll progress. Synchronous scraping endpoints still live in the
``scrape`` router — these are the long-running path.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from magpie.api.deps import get_db_session, get_session_factory_dep
from magpie.queue.tasks import scrape_source_task
from magpie.schemas.jobs import EnqueueResponse, RunItemView, RunView
from magpie.schemas.scrape import ScrapeOnceRequest
from magpie.storage.items_repo_pg import PgItemRepository
from magpie.storage.models import Item, Run
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

    # If deferring the task fails the queued row would otherwise sit forever
    # with no worker ever picking it up. Mark it as errored so the API view is
    # truthful and the operator has a breadcrumb.
    try:
        job_id = await scrape_source_task.defer_async(
            source=source,
            max_items=request.max_items,
            run_id=str(run_id),
        )
    except Exception as exc:
        async with factory() as session:
            await PgRunRepository(session).mark_error(
                run_id, error=f"failed to enqueue task: {exc}"
            )
            await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue scrape task",
        ) from exc

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


def _derive_content_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "content", "body", "summary"):
        val = data.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    if not parts:
        for key in sorted(data.keys()):
            if key in ("id", "url", "html_snapshot_url"):
                continue
            val = data.get(key)
            if isinstance(val, str) and val:
                parts.append(val)
    return "\n".join(parts)


def _item_view(item: Item) -> RunItemView:
    data = item.data or {}
    url_val = data.get("url")
    title_val = data.get("title")
    snapshot = data.get("html_snapshot_url")
    return RunItemView(
        id=item.id,
        stable_id=item.dedupe_key,
        url=str(url_val) if url_val else "",
        title=str(title_val) if title_val else "",
        content_text=_derive_content_text(data),
        content_hash=item.content_hash,
        first_seen_at=item.first_seen_at,
        last_seen_at=item.last_seen_at,
        html_snapshot_url=str(snapshot) if isinstance(snapshot, str) else None,
    )


@router.get("/api/runs/{run_id}/items", response_model=list[RunItemView])
async def list_run_items(
    run_id: uuid.UUID,
    session: _Session,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[RunItemView]:
    """List items persisted during a run's time window.

    Scoped by ``items.last_seen_at ∈ [run.started_at, run.ended_at]`` — captures
    every item the scraper touched in this run, minus items that the same run
    or a later one marked removed.
    """
    run = await PgRunRepository(session).get(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )
    items = await PgItemRepository(session).list_in_window(
        source_id=run.source_id,
        started_at=run.started_at,
        ended_at=run.ended_at,
        limit=limit,
        offset=offset,
    )
    return [_item_view(item) for item in items]
