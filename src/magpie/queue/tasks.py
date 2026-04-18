"""Async tasks registered with the magpie Procrastinate app.

Each task is a thin wrapper around the service layer: it resolves the process-
wide session factory, calls the pure service function, and on success decides
whether to enqueue a follow-up (e.g. healer-on-underflow). Business logic
stays in ``scrape_service`` / ``healer.apply`` — not here — so the tasks remain
trivially testable with ``InMemoryConnector``.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from procrastinate import RetryStrategy

from magpie.queue.app import queue_app
from magpie.services.scrape_service import ScrapeExecutionError, scrape_once
from magpie.storage.db import get_session_factory
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository

# A run that's been in ``running`` for longer than this is almost certainly
# orphaned by a worker crash or a Render free-tier sleep. The reaper moves
# those to ``error`` so the UI shows the truth.
STALE_RUN_SECONDS = 30 * 60


@queue_app.task(
    name="magpie.scrape_source",
    queue="scrape",
    retry=RetryStrategy(
        max_attempts=3,
        exponential_wait=2,
        retry_exceptions={httpx.HTTPError, httpx.TimeoutException, ScrapeExecutionError},
    ),
)
async def scrape_source_task(
    *,
    source: str,
    max_items: int = 20,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute one scrape run. ``run_id`` ties back to a pre-created runs row."""
    factory = get_session_factory()
    parsed_run_id = uuid.UUID(run_id) if run_id else None

    result = await scrape_once(
        source=source,
        max_items=max_items,
        session_factory=factory,
        run_id=parsed_run_id,
    )

    # If the run underflowed the source's health threshold, enqueue a heal.
    async with factory() as session:
        repo = SourcesRepository(session)
        config = await repo.get_config(source)
    min_items = config.health.min_items
    if min_items > 0 and len(result.items) < min_items:
        await heal_source_task.defer_async(
            source=source,
            run_id=str(result.run_id),
        )

    return {
        "run_id": str(result.run_id),
        "source": source,
        "item_count": len(result.items),
    }


@queue_app.task(
    name="magpie.heal_source",
    queue="heal",
    retry=RetryStrategy(max_attempts=2, exponential_wait=5),
)
async def heal_source_task(
    *,
    source: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run the healer pipeline for a source whose last run fell short."""
    # Late import so importing ``magpie.queue.tasks`` doesn't pull the healer
    # stack (and httpx->OpenRouter client) in just to register the task.
    from magpie.healer.apply import heal_source

    factory = get_session_factory()
    parsed_run_id = uuid.UUID(run_id) if run_id else None
    outcome = await heal_source(
        source=source,
        run_id=parsed_run_id,
        session_factory=factory,
    )
    return outcome


@queue_app.periodic(cron="*/15 * * * *")
@queue_app.task(
    name="magpie.reap_stale_runs",
    queue="maintenance",
)
async def reap_stale_runs_task(timestamp: int) -> dict[str, Any]:
    """Mark long-running rows as ``error`` — runs every 15 minutes.

    Procrastinate's periodic decorator invokes the task with a unix
    ``timestamp`` positional argument; we don't use it, but the parameter is
    required by the decorator contract.
    """
    factory = get_session_factory()
    async with factory() as session:
        reaped = await PgRunRepository(session).mark_stale_running_as_error(
            older_than_seconds=STALE_RUN_SECONDS,
        )
        await session.commit()
    return {"reaped": reaped, "threshold_seconds": STALE_RUN_SECONDS}


__all__ = ["heal_source_task", "reap_stale_runs_task", "scrape_source_task"]
