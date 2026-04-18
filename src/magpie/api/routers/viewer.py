"""DB-backed ``/sources``, ``/runs``, ``/heals`` endpoints for the frontend.

These replace the in-memory demo data that used to live in ``main.py``.
Shapes are preserved so the existing magpie-frontend keeps working, aside
from ``run.id`` and ``heal.id`` which are now UUIDs rather than ints.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from magpie.api.deps import get_db_session
from magpie.storage.heals_repo import HealsRepository
from magpie.storage.models import Item, Run, RunStatus, Source
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository, SourceStats

router = APIRouter(tags=["viewer"])

_Session = Annotated[AsyncSession, Depends(get_db_session)]

_SOURCE_NAME_RE = re.compile(r"^[a-z0-9-]+$")


# ── Response models ──────────────────────────────────────────────────────────


class SourceView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    description: str = ""
    last_run_at: datetime | None = None
    last_status: str | None = None
    item_count: int = 0
    config_sha: str = ""


class RunView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    source: str
    started_at: datetime
    ended_at: datetime | None = None
    items_new: int = 0
    items_updated: int = 0
    items_removed: int = 0
    status: str = ""
    error: str | None = None


class HealView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    source: str
    run_id: UUID | None = None
    old_config: dict[str, Any]
    new_config: dict[str, Any]
    pr_url: str | None = None
    created_at: datetime


# ── Helpers ──────────────────────────────────────────────────────────────────


def _source_view_from_stats(stats: SourceStats) -> SourceView:
    return SourceView(
        name=stats.source.name,
        description=stats.source.description,
        last_run_at=stats.last_run_at,
        last_status=stats.last_status.value if stats.last_status else None,
        item_count=stats.item_count,
        config_sha=stats.source.config_sha,
    )


async def _source_view(session: AsyncSession, source: Source) -> SourceView:
    """Single-source stats — used by ``GET /sources/{name}`` only."""
    stmt = select(Run).where(Run.source_id == source.id).order_by(Run.started_at.desc()).limit(1)
    latest = (await session.execute(stmt)).scalar_one_or_none()

    count_stmt = (
        select(func.count(Item.id))
        .where(Item.source_id == source.id)
        .where(Item.removed.is_(False))
    )
    item_count = int((await session.execute(count_stmt)).scalar_one())

    return SourceView(
        name=source.name,
        description=source.description,
        last_run_at=latest.started_at if latest else None,
        last_status=latest.status.value if latest else None,
        item_count=item_count,
        config_sha=source.config_sha,
    )


def _run_view(row: Run) -> RunView:
    return RunView(
        id=row.id,
        source=row.source_name,
        started_at=row.started_at,
        ended_at=row.ended_at,
        items_new=row.items_new,
        items_updated=row.items_updated,
        items_removed=row.items_removed,
        status=row.status.value,
        error=row.error,
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/sources", response_model=list[SourceView])
async def list_sources(session: _Session) -> list[SourceView]:
    """List every source with its latest run + item count.

    Uses :meth:`SourcesRepository.list_with_stats` so the query count stays at
    three regardless of source count (was O(N) before).
    """
    repo = SourcesRepository(session)
    return [_source_view_from_stats(stats) for stats in await repo.list_with_stats()]


@router.get("/sources/{name}", response_model=SourceView)
async def get_source(name: str, session: _Session) -> SourceView:
    if not _SOURCE_NAME_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid source name format",
        )
    repo = SourcesRepository(session)
    row = await repo.get_by_name(name)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return await _source_view(session, row)


@router.get("/runs", response_model=list[RunView])
async def list_runs(
    session: _Session,
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunView]:
    repo = PgRunRepository(session)
    rows = await repo.list_runs(source_name=source, limit=limit, offset=offset)
    return [_run_view(r) for r in rows]


@router.get("/heals", response_model=list[HealView])
async def list_heals(
    session: _Session,
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[HealView]:
    repo = HealsRepository(session)
    rows = await repo.list_all_with_source(source_name=source, limit=limit, offset=offset)
    return [
        HealView(
            id=heal.id,
            source=source_name,
            run_id=heal.run_id,
            old_config={"field": heal.field_name, "selector": heal.old_selector},
            new_config={"field": heal.field_name, "selector": heal.new_selector},
            pr_url=heal.pr_url,
            created_at=heal.created_at,
        )
        for heal, source_name in rows
    ]


__all__ = ["RunStatus", "router"]
