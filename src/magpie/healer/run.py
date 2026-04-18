"""``python -m magpie.healer.run`` — GitHub-Action entrypoint.

Finds the most recent failed runs in Postgres, groups them by source, and
invokes ``healer.apply.heal_source`` on each. File-origin sources end up with
a PR; api-origin sources get patched in place.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from magpie.healer.apply import heal_source
from magpie.storage.db import get_session_factory
from magpie.storage.runs_repo_pg import PgRunRepository

log = logging.getLogger("magpie.healer.run")


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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_heal_recent_failures())


if __name__ == "__main__":
    sys.exit(main())
