"""POST /api/scrape/{source}/once and POST /api/scrape/batch.

Thin translation layer: parse request → call service → map typed result /
exceptions to HTTP responses. No DB or scraping logic lives here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from magpie.api.deps import get_session_factory_dep
from magpie.schemas.scrape import (
    ScrapeBatchRequest,
    ScrapeBatchResponse,
    ScrapeFailure,
    ScrapeOnceRequest,
    ScrapeResult,
)
from magpie.services.scrape_service import (
    ScrapeExecutionError,
    UnknownSourceError,
    scrape_batch,
    scrape_once,
)

router = APIRouter(prefix="/api/scrape", tags=["scrape"])

_Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory_dep)]


@router.post("/{source}/once", response_model=ScrapeResult)
async def scrape_source_once(
    source: str,
    factory: _Factory,
    body: ScrapeOnceRequest | None = None,
) -> ScrapeResult:
    """Synchronously run one registered scraper and return its items."""
    request = body or ScrapeOnceRequest()
    try:
        return await scrape_once(
            source=source,
            max_items=request.max_items,
            session_factory=factory,
        )
    except UnknownSourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown source: {exc.source}",
        ) from exc
    except ScrapeExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post("/batch", response_model=ScrapeBatchResponse)
async def scrape_sources_batch(body: ScrapeBatchRequest, factory: _Factory) -> ScrapeBatchResponse:
    """Synchronously run multiple registered scrapers concurrently."""
    runs, failed = await scrape_batch(
        sources=tuple(body.sources),
        max_items_per_source=body.max_items_per_source,
        session_factory=factory,
    )
    return ScrapeBatchResponse(
        runs=tuple(runs),
        failed=tuple(ScrapeFailure(**f) for f in failed),
    )
