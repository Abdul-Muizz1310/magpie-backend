"""``python -m magpie.healer.run`` — GitHub-Action entrypoint.

Default behaviour: find the most recent failed runs in Postgres, group them by
source, and invoke ``healer.apply.heal_source`` on each. File-origin sources
end up with a PR; api-origin sources get patched in place.

Manual-dispatch override: if ``HEAL_SOURCE_FILTER`` is set in the environment,
heal that one source directly (no failed-run row required). Lets the
``heal-on-failure`` workflow's ``workflow_dispatch`` kick the healer at a
specific scraper on demand — useful when a selector has drifted but the scrape
exits 0 because a handful of items still make it through.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from magpie.healer.apply import heal_source
from magpie.healer.detector import should_heal
from magpie.storage.db import get_session_factory
from magpie.storage.models import RunStatus
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository

log = logging.getLogger("magpie.healer.run")


async def _heal_one_source(source: str) -> int:
    factory = get_session_factory()
    log.info("manual-dispatch healing %s (no run context)", source)
    summary = await heal_source(source=source, run_id=None, session_factory=factory)
    log.info("heal summary: %s", summary)
    return 0


async def _underflowed_sources(session: AsyncSession) -> dict[str, uuid.UUID]:
    """Sources whose most-recent OK run returned fewer items than min_items.

    Silent selector drift marks runs ``ok`` (the fetch succeeded) but yields
    near-empty results, so ``latest_failed_runs`` alone misses exactly the case
    the healer exists for (MAG-1). Map each such source to its latest run id.
    """
    sources_repo = SourcesRepository(session)
    run_repo = PgRunRepository(session)
    result: dict[str, uuid.UUID] = {}
    for src in await sources_repo.list_all():
        runs = await run_repo.list_runs(source_name=src.name, limit=1)
        if not runs or runs[0].status is not RunStatus.ok:
            continue
        latest = runs[0]
        try:
            config = await sources_repo.get_config(src.name)
        except Exception:
            log.warning("skipping %s: config no longer valid", src.name)
            continue
        if should_heal(item_count=latest.item_count, min_items=config.health.min_items):
            result[src.name] = latest.id
    return result


async def _heal_recent_failures() -> int:
    factory = get_session_factory()
    async with factory() as session:
        failed_runs = await PgRunRepository(session).latest_failed_runs()
        underflowed = await _underflowed_sources(session)

    # Failed runs first (explicit errors), then silent-underflow sources.
    targets: dict[str, uuid.UUID | None] = {}
    for run in failed_runs:
        targets.setdefault(run.source_name, run.id)
    for uf_name, uf_run_id in underflowed.items():
        targets.setdefault(uf_name, uf_run_id)

    if not targets:
        print("no failed or underflowed runs to heal")
        return 0

    for name, run_id in targets.items():
        log.info("healing %s (run %s)", name, run_id)
        summary = await heal_source(
            source=name,
            run_id=run_id,
            session_factory=factory,
        )
        log.info("heal summary: %s", summary)
    return 0


async def _main() -> int:
    source_filter = os.environ.get("HEAL_SOURCE_FILTER", "").strip()
    if source_filter:
        return await _heal_one_source(source_filter)
    return await _heal_recent_failures()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
