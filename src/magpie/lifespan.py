"""FastAPI lifespan for magpie.

Two startup responsibilities, in order:

1. **Sync file-origin sources into Postgres.** Any YAML that ships in
   ``configs/`` becomes an ``origin=file`` row so the API layer can treat all
   sources uniformly — no file-vs-db split at read time.
2. **Start the embedded Procrastinate worker.** Render's free tier charges
   for background workers, so magpie runs the worker in-process; the task
   gets cancelled on shutdown with ``shutdown_graceful_timeout`` seconds to
   drain.

``_sync_file_sources_to_db`` is exposed so the CLI ``migrate`` subcommand can
call it outside a lifespan.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from magpie.config.loader import load_config
from magpie.paths import configs_dir
from magpie.queue.app import queue_app
from magpie.storage.db import get_session_factory
from magpie.storage.sources_repo import SourcesRepository

log = logging.getLogger("magpie.lifespan")

WORKER_CONCURRENCY = 3
WORKER_GRACEFUL_SHUTDOWN_SEC = 20.0


async def _sync_file_sources_to_db() -> int:
    """Upsert every ``configs/*.yaml`` into the ``sources`` table.

    Returns the number of files synced. Silently skips files that fail
    Pydantic validation — those are already logged by the loader.
    """
    cfg_dir = configs_dir()
    if not cfg_dir.is_dir():
        return 0

    factory = get_session_factory()
    synced = 0
    async with factory() as session:
        repo = SourcesRepository(session)
        for yaml_file in sorted(cfg_dir.glob("*.yaml")):
            try:
                text = yaml_file.read_text(encoding="utf-8")
                config = load_config(text)
            except Exception:
                log.exception("Skipping invalid config %s", yaml_file)
                continue
            await repo.upsert_file_source(config=config, yaml_text=text)
            synced += 1
        await session.commit()
    return synced


@asynccontextmanager
async def magpie_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan that owns both the worker task and the source sync."""
    try:
        await _sync_file_sources_to_db()
    except Exception:
        log.exception("Failed to sync file-origin sources; continuing startup")

    worker_task: asyncio.Task[None] | None = None
    try:
        await queue_app.open_async()
        worker_task = asyncio.create_task(
            queue_app.run_worker_async(
                install_signal_handlers=False,
                concurrency=WORKER_CONCURRENCY,
                shutdown_graceful_timeout=WORKER_GRACEFUL_SHUTDOWN_SEC,
            )
        )
    except Exception:
        log.exception("Procrastinate worker failed to start; API will still serve")

    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(worker_task, timeout=WORKER_GRACEFUL_SHUTDOWN_SEC + 5)
        try:
            await queue_app.close_async()
        except Exception:
            log.exception("Error closing Procrastinate app")


__all__ = ["_sync_file_sources_to_db", "magpie_lifespan"]
