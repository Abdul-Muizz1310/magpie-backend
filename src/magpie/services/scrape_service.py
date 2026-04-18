"""Scrape orchestration — config lookup, runner dispatch, persistence.

The router layer calls into this module only. Keeping the pure async shape
here means tests can drive it without spinning up HTTP or a browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from magpie.config.loader import load_config_from_file
from magpie.config.schema import SourceConfig
from magpie.schemas.scrape import ScrapeFailure, ScrapeItem, ScrapeResult
from magpie.scrapy.factory import run_spider
from magpie.storage.run_repo import RunRecord, RunRepository

# ── Typed exceptions ────────────────────────────────────────────────────────


class UnknownSourceError(Exception):
    """Raised when the caller asks for a source that is not registered."""

    def __init__(self, source: str) -> None:
        super().__init__(f"Unknown source: {source}")
        self.source = source


class ScrapeExecutionError(Exception):
    """Raised when the underlying scraper runner fails hard."""


# ── Config registry lookup (side-effecting, lives at the edge) ──────────────

_CONFIGS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "configs"


def _get_registered_source(name: str) -> SourceConfig:
    """Resolve ``name`` to its validated ``SourceConfig`` or raise."""
    if not _CONFIGS_DIR.is_dir():
        raise UnknownSourceError(name)

    target = _CONFIGS_DIR / f"{name}.yaml"
    if not target.is_file():
        raise UnknownSourceError(name)

    try:
        return load_config_from_file(target)
    except Exception as exc:
        raise UnknownSourceError(name) from exc


# ── Runner execution (imperative shell; replaced by mocks in tests) ─────────


async def _execute_static(config: SourceConfig, max_items: int) -> list[dict[str, Any]]:
    """Run a Scrapy (static) scrape in a worker thread, cap to ``max_items``."""
    raw = await asyncio.to_thread(run_spider, config)
    return list(raw[:max_items])


async def _execute_js(config: SourceConfig, max_items: int) -> list[dict[str, Any]]:
    """Run a Playwright (JS-rendered) scrape, cap to ``max_items``."""
    from magpie.playwright.runner import PlaywrightRunner

    runner = PlaywrightRunner(config)
    raw = await runner.run()
    return list(raw[:max_items])


# ── Pure helpers ────────────────────────────────────────────────────────────


def _derive_content_text(item: dict[str, Any]) -> str:
    """Build the response ``content_text`` from a scraped item.

    Prefers ``title`` + ``content`` when present; otherwise joins all non-None
    string field values. Deterministic so the content hash is stable.
    """
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
    """Return (NFC-normalised text, SHA-256 of the normalised text)."""
    nfc = unicodedata.normalize("NFC", text)
    digest = hashlib.sha256(nfc.encode("utf-8")).hexdigest()
    return nfc, digest


def _item_from_raw(raw: dict[str, Any], config: SourceConfig) -> ScrapeItem:
    """Project a raw scraped dict into the API response shape."""
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


# ── Public service API ──────────────────────────────────────────────────────


async def scrape_once(
    *,
    source: str,
    max_items: int,
    run_repo: RunRepository,
) -> ScrapeResult:
    """Run one registered scraper, persist the run, return a typed result."""
    config = _get_registered_source(source)
    run_id = uuid.uuid4()
    started_at = datetime.now(UTC)

    try:
        raw_items = (
            await _execute_js(config, max_items)
            if config.render
            else await _execute_static(config, max_items)
        )
    except Exception as exc:
        ended_at = datetime.now(UTC)
        run_repo.record_run(
            RunRecord(
                run_id=str(run_id),
                source=config.name,
                started_at=started_at,
                ended_at=ended_at,
                item_count=0,
                duration_ms=_duration_ms(started_at, ended_at),
                status="error",
                error=str(exc),
            )
        )
        raise ScrapeExecutionError(str(exc)) from exc

    items = tuple(_item_from_raw(raw, config) for raw in raw_items)
    ended_at = datetime.now(UTC)
    run_repo.record_run(
        RunRecord(
            run_id=str(run_id),
            source=config.name,
            started_at=started_at,
            ended_at=ended_at,
            item_count=len(items),
            duration_ms=_duration_ms(started_at, ended_at),
            status="ok",
            error=None,
        )
    )

    return ScrapeResult(
        source=config.name,
        scraped_at=started_at,
        run_id=run_id,
        items=items,
    )


async def scrape_batch(
    *,
    sources: tuple[str, ...],
    max_items_per_source: int,
    run_repo: RunRepository,
) -> tuple[list[ScrapeResult], list[dict[str, str]]]:
    """Run multiple scrapers concurrently with per-source isolation.

    Returns (successful results, failure records). One source raising does
    not abort the batch.
    """
    tasks = [
        scrape_once(source=name, max_items=max_items_per_source, run_repo=run_repo)
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


# ── Small internal helper ───────────────────────────────────────────────────


def _duration_ms(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


# Expose ScrapeFailure to ease router-layer imports in the future.
__all__ = [
    "ScrapeExecutionError",
    "ScrapeFailure",
    "UnknownSourceError",
    "scrape_batch",
    "scrape_once",
]
