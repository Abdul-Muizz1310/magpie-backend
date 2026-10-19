"""``python -m magpie.healer.run`` — GitHub-Action entrypoint.

Default behaviour: find every source whose *latest* run failed or underflowed
``health.min_items`` and invoke ``healer.apply.heal_source`` on each. File-origin
sources end up with a PR; api-origin sources get patched in place.

Manual-dispatch override: if ``HEAL_SOURCE_FILTER`` is set in the environment,
heal that one source directly (no failed-run row required). Lets the
``heal-on-failure`` workflow's ``workflow_dispatch`` kick the healer at a
specific scraper on demand — useful when a selector has drifted but the scrape
exits 0 because a handful of items still make it through.

Exit status (spec 03, "A heal ends healed or failed"): **1** when any heal
target ends without a landed fix, so the workflow job is red; 0 when every
target healed or there was no target. Each failed source is logged at ERROR.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from magpie.healer.apply import heal_source
from magpie.healer.detector import should_heal
from magpie.healer.outcome import HealSummary
from magpie.storage.db import get_session_factory
from magpie.storage.models import RunStatus
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository

log = logging.getLogger("magpie.healer.run")

EXIT_OK = 0
EXIT_HEAL_FAILED = 1


def exit_code(summaries: Sequence[HealSummary]) -> int:
    """1 when any summary failed, else 0 (an empty sequence means no target)."""
    return EXIT_HEAL_FAILED if any(not s.succeeded for s in summaries) else EXIT_OK


def _report(summary: HealSummary) -> None:
    if summary.succeeded:
        log.info(
            "healed %s: %s",
            summary.source,
            ", ".join(f"{h.target} -> {h.pr_url or h.mode.value}" for h in summary.healed),
        )
        return
    log.error("heal FAILED for %s: %s", summary.source, summary.describe_failures())


async def _heal_one_source(source: str) -> int:
    factory = get_session_factory()
    log.info("manual-dispatch healing %s (no run context)", source)
    summary = await heal_source(source=source, run_id=None, session_factory=factory)
    _report(summary)
    return exit_code([summary])


async def _heal_targets(session: AsyncSession) -> dict[str, uuid.UUID]:
    """Sources whose *latest* run errored or underflowed, mapped to that run's id.

    Only the latest run counts: a source that errored weeks ago and has scraped
    fine since is not broken (the 2026-10-19 job re-healed every source that had
    ever errored). Silent selector drift marks runs ``ok`` (the fetch succeeded)
    but yields near-empty results, so an ``ok`` run below ``min_items`` is a
    target too (MAG-1).
    """
    sources_repo = SourcesRepository(session)
    run_repo = PgRunRepository(session)
    result: dict[str, uuid.UUID] = {}
    for src in await sources_repo.list_all():
        runs = await run_repo.list_runs(source_name=src.name, limit=1)
        if not runs:
            continue
        latest = runs[0]
        if latest.status is RunStatus.error:
            result[src.name] = latest.id
            continue
        if latest.status is not RunStatus.ok:
            continue
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
        targets = await _heal_targets(session)

    if not targets:
        print("no failed or underflowed runs to heal")
        return EXIT_OK

    summaries: list[HealSummary] = []
    for name, run_id in targets.items():
        log.info("healing %s (run %s)", name, run_id)
        summary = await heal_source(source=name, run_id=run_id, session_factory=factory)
        _report(summary)
        summaries.append(summary)

    failed = [s.source for s in summaries if not s.succeeded]
    if failed:
        log.error("%d of %d heal target(s) failed: %s", len(failed), len(summaries), failed)
    return exit_code(summaries)


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
