"""POST /api/scrape/{source}/once and POST /api/scrape/batch.

Router is a thin translation layer: parse request → call service → map typed
result/exceptions to HTTP responses. No DB or scraping logic lives here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

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
from magpie.storage.run_repo import RunRepository

router = APIRouter(prefix="/api/scrape", tags=["scrape"])

# Single process-wide repo. When a Postgres-backed implementation lands, swap
# this for a FastAPI Depends() that yields a session-scoped instance.
_run_repo = RunRepository()


@router.post("/{source}/once", response_model=ScrapeResult)
async def scrape_source_once(
    source: str,
    body: ScrapeOnceRequest | None = None,
) -> ScrapeResult:
    """Synchronously run one registered scraper and return its items."""
    request = body or ScrapeOnceRequest()

    try:
        return await scrape_once(
            source=source,
            max_items=request.max_items,
            run_repo=_run_repo,
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
async def scrape_sources_batch(body: ScrapeBatchRequest) -> ScrapeBatchResponse:
    """Synchronously run multiple registered scrapers concurrently."""
    runs, failed = await scrape_batch(
        sources=tuple(body.sources),
        max_items_per_source=body.max_items_per_source,
        run_repo=_run_repo,
    )
    return ScrapeBatchResponse(
        runs=tuple(runs),
        failed=tuple(ScrapeFailure(**f) for f in failed),
    )
