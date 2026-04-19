"""Scrape orchestration — DB-backed runs, configs, and items.

The router and the queue task both call this module. Repositories are read
from and written to inside short-lived sessions created from the supplied
``async_sessionmaker`` so the service owns its own transaction boundaries —
important because a single scrape may run for tens of seconds, and we don't
want to hold a DB transaction open the whole time.
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from magpie.config.schema import SourceConfig
from magpie.schemas.scrape import ScrapeFailure, ScrapeItem, ScrapeResult
from magpie.scrapy.factory import run_spider
from magpie.storage.items_repo_pg import PgItemRepository
from magpie.storage.models import Source
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourceNotFoundError, SourcesRepository

# ── Typed exceptions ────────────────────────────────────────────────────────


class UnknownSourceError(Exception):
    """Raised when the caller asks for a source that is not registered."""

    def __init__(self, source: str) -> None:
        super().__init__(f"Unknown source: {source}")
        self.source = source


class ScrapeExecutionError(Exception):
    """Raised when the underlying scraper runner fails hard."""


# ── Runner execution (imperative shell; replaced by mocks in tests) ─────────


async def _execute_static(config: SourceConfig, max_items: int) -> list[dict[str, Any]]:
    """Run a static (httpx + parsel) scrape off the event loop and cap to ``max_items``."""
    raw = await asyncio.to_thread(run_spider, config)
    return list(raw[:max_items])


async def _execute_js(config: SourceConfig, max_items: int) -> list[dict[str, Any]]:
    """Run a Playwright scrape, cap to ``max_items``."""
    from magpie.playwright.runner import PlaywrightRunner

    runner = PlaywrightRunner(config)
    raw = await runner.run()
    return list(raw[:max_items])


async def _execute(config: SourceConfig, max_items: int) -> list[dict[str, Any]]:
    return await (
        _execute_js(config, max_items) if config.render else _execute_static(config, max_items)
    )


# ── Pure helpers ────────────────────────────────────────────────────────────


def _derive_content_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "content", "body", "summary"):
        val = item.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    if not parts:
        for key in sorted(item.keys()):
            if key in ("id", "url", "html_snapshot_url"):
                continue
            val = item.get(key)
            if isinstance(val, str) and val:
                parts.append(val)
    return "\n".join(parts)


def _normalize_and_hash(text: str) -> tuple[str, str]:
    nfc = unicodedata.normalize("NFC", text)
    digest = hashlib.sha256(nfc.encode("utf-8")).hexdigest()
    return nfc, digest


def _deduplicate_items(items: list[dict[str, Any]], dedupe_key: str) -> list[dict[str, Any]]:
    """First-wins dedup on ``dedupe_key``; drop items missing or blank-keyed.

    Wikipedia's Current Events portal (and plenty of other sources) repeats
    the same link across multiple bullets — if we passed both rows through to
    ``persist_items`` the unique-constraint check would reject the whole batch
    and the run would error out instead of returning what it could.

    Rows whose ``dedupe_key`` is missing or blank are dropped too: if we kept
    them, every "no-anchor" bullet would collapse into a single empty-key row
    and we'd lose data that might have been meaningful via other fields.
    """
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item.get(dedupe_key)
        if key is None:
            continue
        k = str(key).strip()
        if not k:
            continue
        if k not in seen:
            seen[k] = item
    return list(seen.values())


def _item_from_raw(raw: dict[str, Any], config: SourceConfig) -> ScrapeItem:
    dedupe_val = raw.get(config.item.dedupe_key)
    stable_id = str(dedupe_val) if dedupe_val is not None else ""
    url_val = raw.get("url")
    title_val = raw.get("title")
    content_source = _derive_content_text(raw)
    content_text, content_hash = _normalize_and_hash(content_source)
    return ScrapeItem(
        stable_id=stable_id or content_hash,
        url=str(url_val) if url_val else "",
        title=str(title_val) if title_val else "",
        content_text=content_text,
        content_hash=content_hash,
        fetched_at=datetime.now(UTC),
        html_snapshot_url=(
            str(raw["html_snapshot_url"]) if isinstance(raw.get("html_snapshot_url"), str) else None
        ),
    )


# ── Internal helpers for the DB-backed pipeline ─────────────────────────────


async def _resolve_source(session: AsyncSession, name: str) -> tuple[Source, SourceConfig]:
    repo = SourcesRepository(session)
    row = await repo.get_by_name(name)
    if row is None:
        raise UnknownSourceError(name)
    try:
        config = await repo.get_config(name)
    except SourceNotFoundError as exc:
        raise UnknownSourceError(name) from exc
    except (ValueError, ValidationError) as exc:
        # Stored YAML was valid at write time but no longer validates — treat
        # like an unknown source so the caller gets a clean 404/UnknownSource
        # rather than a 500.
        raise UnknownSourceError(name) from exc
    return row, config


# ── Public service API ──────────────────────────────────────────────────────


async def scrape_once(
    *,
    source: str,
    max_items: int,
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID | None = None,
) -> ScrapeResult:
    """Run a scraper, record the run, persist its items.

    If ``run_id`` is supplied, the existing queued row is reused (this is how
    the queue task flows — the enqueue path pre-creates the row so the caller
    has a handle before the task starts). Otherwise a fresh row is created
    here, mirroring the behaviour of the legacy sync endpoint.
    """
    async with session_factory() as session:
        source_row, config = await _resolve_source(session, source)
        runs = PgRunRepository(session)

        if run_id is None:
            run = await runs.create_queued(
                source_id=source_row.id,
                source_name=source_row.name,
            )
            run_id = run.id
        await runs.mark_running(run_id)
        await session.commit()

    started_at = datetime.now(UTC)
    try:
        raw_items = await _execute(config, max_items)
    except Exception as exc:
        async with session_factory() as session:
            runs = PgRunRepository(session)
            await runs.mark_error(run_id, error=str(exc), started_at=started_at)
            await session.commit()
        raise ScrapeExecutionError(str(exc)) from exc

    raw_items = _deduplicate_items(raw_items, config.item.dedupe_key)

    async with session_factory() as session:
        items_repo = PgItemRepository(session)
        persist = await items_repo.persist_items(
            source_row.id,
            raw_items,
            dedupe_key=config.item.dedupe_key,
        )
        runs = PgRunRepository(session)
        scrape_items = tuple(_item_from_raw(raw, config) for raw in raw_items)
        await runs.mark_ok(
            run_id,
            item_count=len(scrape_items),
            items_new=persist.items_new,
            items_updated=persist.items_updated,
            items_removed=persist.items_removed,
            started_at=started_at,
        )
        await session.commit()

    return ScrapeResult(
        source=source_row.name,
        scraped_at=started_at,
        run_id=run_id,
        items=scrape_items,
    )


async def scrape_batch(
    *,
    sources: tuple[str, ...],
    max_items_per_source: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[list[ScrapeResult], list[dict[str, str]]]:
    """Run multiple scrapers concurrently; one failure does not abort the rest."""
    tasks = [
        scrape_once(
            source=name,
            max_items=max_items_per_source,
            session_factory=session_factory,
        )
        for name in sources
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    runs: list[ScrapeResult] = []
    failed: list[dict[str, str]] = []
    for name, outcome in zip(sources, raw_results, strict=True):
        if isinstance(outcome, BaseException):
            failed.append({"source": name, "error": str(outcome)})
            continue
        runs.append(outcome)

    return runs, failed


__all__ = [
    "ScrapeExecutionError",
    "ScrapeFailure",
    "UnknownSourceError",
    "scrape_batch",
    "scrape_once",
]
