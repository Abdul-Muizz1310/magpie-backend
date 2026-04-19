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

from magpie.healer.apply import heal_source
from magpie.storage.db import get_session_factory
from magpie.storage.runs_repo_pg import PgRunRepository

log = logging.getLogger("magpie.healer.run")


async def _heal_one_source(source: str) -> int:
    factory = get_session_factory()
    log.info("manual-dispatch healing %s (no run context)", source)
    summary = await heal_source(source=source, run_id=None, session_factory=factory)
    log.info("heal summary: %s", summary)
    return 0


async def _heal_recent_failures() -> int:
    factory = get_session_factory()
    async with factory() as session:
        failed_runs = await PgRunRepository(session).latest_failed_runs()
    if not failed_runs:
        print("no failed runs to heal")
        return 0

    seen: set[str] = set()
    for run in failed_runs:
        if run.source_name in seen:
            continue
        seen.add(run.source_name)
        log.info("healing %s (run %s)", run.source_name, run.id)
        summary = await heal_source(
            source=run.source_name,
            run_id=run.id,
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
