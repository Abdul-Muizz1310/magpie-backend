"""Service-level tests for scrape_service (DB-backed).

These drive ``scrape_once`` / ``scrape_batch`` end-to-end against a SQLite
session factory — the same interface Postgres uses in production — while
mocking only the HTTP/Playwright boundaries.
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import scrapy
import yaml
from sqlalchemy import select

from magpie.config.schema import SourceConfig
from magpie.services.scrape_service import (
    ScrapeExecutionError,
    UnknownSourceError,
    scrape_batch,
    scrape_once,
)
from magpie.storage.models import Run, RunStatus, Source, SourceOrigin
from magpie.storage.sources_repo import SourcesRepository


def _static_config(name: str = "test-static") -> SourceConfig:
    return SourceConfig(
        name=name,
        url="https://example.com",  # type: ignore[arg-type]
        render=False,
        schedule="0 */6 * * *",
        item={  # type: ignore[arg-type]
            "container": "tr.row",
            "fields": [
                {"name": "id", "selector": "::attr(id)"},
                {"name": "title", "selector": "a::text"},
                {"name": "url", "selector": "a::attr(href)"},
            ],
            "dedupe_key": "id",
        },
    )


def _js_config(name: str = "test-js") -> SourceConfig:
    return SourceConfig(
        name=name,
        url="https://example.com",  # type: ignore[arg-type]
        render=True,
        wait_for="div.list",
        schedule="0 */6 * * *",
        item={  # type: ignore[arg-type]
            "container": "div.card",
            "fields": [
                {"name": "id", "selector": "::attr(data-id)"},
                {"name": "title", "selector": "h3::text"},
                {"name": "url", "selector": "a::attr(href)"},
            ],
            "dedupe_key": "id",
        },
    )


async def _seed(
    session_factory, config: SourceConfig, origin: SourceOrigin = SourceOrigin.api
) -> None:
    async with session_factory() as session:
        repo = SourcesRepository(session)
        await repo.create(
            config=config,
            origin=origin,
            yaml_text=yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        )
        await session.commit()


class TestRunnerDispatch:
    async def test_static_config_uses_scrapy_runner(self, session_factory) -> None:
        await _seed(session_factory, _static_config())

        with (
            patch(
                "magpie.services.scrape_service._execute_static",
                new=AsyncMock(
                    return_value=[{"id": "a", "title": "t", "url": "https://example.com/a"}]
                ),
            ) as exec_static,
            patch(
                "magpie.services.scrape_service._execute_js",
                new=AsyncMock(return_value=[]),
            ) as exec_js,
        ):
            result = await scrape_once(
                source="test-static",
                max_items=5,
                session_factory=session_factory,
            )

        assert exec_static.await_count == 1
        assert exec_js.await_count == 0
        assert result.source == "test-static"

    async def test_js_config_uses_playwright_runner(self, session_factory) -> None:
        await _seed(session_factory, _js_config())

        with (
            patch(
                "magpie.services.scrape_service._execute_static",
                new=AsyncMock(return_value=[]),
            ) as exec_static,
            patch(
                "magpie.services.scrape_service._execute_js",
                new=AsyncMock(return_value=[{"id": "a", "title": "t", "url": "u"}]),
            ) as exec_js,
        ):
            result = await scrape_once(
                source="test-js",
                max_items=10,
                session_factory=session_factory,
            )

        assert exec_static.await_count == 0
        assert exec_js.await_count == 1
        assert result.source == "test-js"


class TestRunPersistence:
    async def test_service_writes_run_row(self, session_factory) -> None:
        await _seed(session_factory, _static_config())

        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(
                return_value=[
                    {"id": "a", "title": "t1", "url": "https://example.com/a"},
                    {"id": "b", "title": "t2", "url": "https://example.com/b"},
                ]
            ),
        ):
            result = await scrape_once(
                source="test-static",
                max_items=10,
                session_factory=session_factory,
            )

        async with session_factory() as session:
            rows = (await session.execute(select(Run))).scalars().all()
            assert len(rows) == 1
            row = rows[0]
            assert row.source_name == "test-static"
            assert row.status is RunStatus.ok
            assert row.item_count == 2
            assert row.duration_ms >= 0
            assert row.id == result.run_id


class TestContentNormalisation:
    async def test_content_text_nfc_normalised_before_hash(self, session_factory) -> None:
        await _seed(session_factory, _static_config())

        nfd_text = "caf" + "e" + "\u0301"
        nfc_text = unicodedata.normalize("NFC", nfd_text)
        assert nfd_text != nfc_text

        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(
                return_value=[{"id": "1", "title": nfd_text, "url": "https://example.com/1"}]
            ),
        ):
            result = await scrape_once(
                source="test-static",
                max_items=10,
                session_factory=session_factory,
            )

        expected = hashlib.sha256(nfc_text.encode("utf-8")).hexdigest()
        assert result.items[0].content_hash == expected
        assert result.items[0].content_text == nfc_text


class TestDeduplicateItems:
    """Unit tests for the first-wins dedup helper exercised by ``scrape_once``."""

    def test_first_occurrence_wins(self) -> None:
        from magpie.services.scrape_service import _deduplicate_items

        items = [
            {"id": "a", "title": "first"},
            {"id": "a", "title": "second — should be dropped"},
            {"id": "b", "title": "b1"},
        ]
        deduped = _deduplicate_items(items, "id")
        assert deduped == [
            {"id": "a", "title": "first"},
            {"id": "b", "title": "b1"},
        ]

    def test_items_missing_dedupe_key_are_dropped(self) -> None:
        from magpie.services.scrape_service import _deduplicate_items

        items = [
            {"id": "a", "title": "has id"},
            {"title": "no id"},
            {"id": None, "title": "null id"},
        ]
        deduped = _deduplicate_items(items, "id")
        assert deduped == [{"id": "a", "title": "has id"}]

    def test_empty_batch(self) -> None:
        from magpie.services.scrape_service import _deduplicate_items

        assert _deduplicate_items([], "id") == []

    def test_numeric_keys_compared_as_strings(self) -> None:
        """An int ``1`` and str ``"1"`` must hash to the same dedupe key."""
        from magpie.services.scrape_service import _deduplicate_items

        items = [
            {"id": 1, "title": "as int"},
            {"id": "1", "title": "as str"},
        ]
        deduped = _deduplicate_items(items, "id")
        assert len(deduped) == 1

    def test_items_with_blank_dedupe_key_are_dropped(self) -> None:
        """Blank/whitespace-only keys would otherwise collapse real rows into
        a single empty-key row. Drop them so the rest persist cleanly.
        """
        from magpie.services.scrape_service import _deduplicate_items

        items = [
            {"id": "", "title": "blank anchor"},
            {"id": "   ", "title": "whitespace only"},
            {"id": "a", "title": "real"},
        ]
        deduped = _deduplicate_items(items, "id")
        assert deduped == [{"id": "a", "title": "real"}]


class TestScrapeItemSchema:
    """Regression guards for ScrapeItem's field constraints.

    The wikipedia-current-events source yields valid bullets that have no
    anchor (hence no url); those used to crash the whole batch when
    ``url: str = Field(min_length=1)``.
    """

    def test_accepts_item_with_empty_url_and_title(self) -> None:
        from datetime import UTC, datetime

        from magpie.schemas.scrape import ScrapeItem

        # No ValidationError expected.
        item = ScrapeItem(
            stable_id="abc",
            url="",
            title="",
            content_text="plain bullet",
            content_hash="0" * 64,
            fetched_at=datetime.now(UTC),
        )
        assert item.url == ""
        assert item.title == ""

    def test_still_rejects_empty_stable_id_and_hash(self) -> None:
        """``stable_id`` and ``content_hash`` stay non-empty — those are the
        only things we need to persist + dedupe rows, so an empty value is a
        real bug.
        """
        from datetime import UTC, datetime

        from pydantic import ValidationError

        from magpie.schemas.scrape import ScrapeItem

        with pytest.raises(ValidationError):
            ScrapeItem(
                stable_id="",
                url="https://x",
                title="t",
                content_text="c",
                content_hash="0" * 64,
                fetched_at=datetime.now(UTC),
            )
        with pytest.raises(ValidationError):
            ScrapeItem(
                stable_id="abc",
                url="https://x",
                title="t",
                content_text="c",
                content_hash="",
                fetched_at=datetime.now(UTC),
            )


class TestEmptyItems:
    async def test_empty_items_returns_gracefully(self, session_factory) -> None:
        await _seed(session_factory, _static_config())
        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(return_value=[]),
        ):
            result = await scrape_once(
                source="test-static",
                max_items=10,
                session_factory=session_factory,
            )
        assert result.items == ()
        async with session_factory() as session:
            rows = (await session.execute(select(Run))).scalars().all()
            assert len(rows) == 1
            assert rows[0].item_count == 0
            assert rows[0].status is RunStatus.ok


class TestItemRepoPersistence:
    async def test_persist_counts_flow_into_run_record(self, session_factory) -> None:
        await _seed(session_factory, _static_config())

        first_batch = [
            {"id": "a", "title": "A1", "url": "https://example.com/a"},
            {"id": "b", "title": "B1", "url": "https://example.com/b"},
        ]
        second_batch = [
            {"id": "a", "title": "A2 updated", "url": "https://example.com/a"},
            {"id": "c", "title": "C1", "url": "https://example.com/c"},
        ]

        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(side_effect=[first_batch, second_batch]),
        ):
            await scrape_once(
                source="test-static",
                max_items=10,
                session_factory=session_factory,
            )
            await scrape_once(
                source="test-static",
                max_items=10,
                session_factory=session_factory,
            )

        async with session_factory() as session:
            rows = (await session.execute(select(Run).order_by(Run.created_at))).scalars().all()
            assert len(rows) == 2
            assert (rows[0].items_new, rows[0].items_updated, rows[0].items_removed) == (
                2,
                0,
                0,
            )
            assert (rows[1].items_new, rows[1].items_updated, rows[1].items_removed) == (
                1,
                1,
                1,
            )


class TestUnknownSource:
    async def test_unknown_source_raises(self, session_factory) -> None:
        with pytest.raises(UnknownSourceError):
            await scrape_once(
                source="ghost",
                max_items=5,
                session_factory=session_factory,
            )


class TestBatchConcurrency:
    async def test_batch_isolates_one_failing_source(self, session_factory) -> None:
        for name in ("first", "middle", "last"):
            await _seed(session_factory, _static_config(name))

        async def _fake_execute(config: SourceConfig, max_items: int) -> list[dict[str, Any]]:
            if config.name == "middle":
                await asyncio.sleep(0)
                raise RuntimeError("boom")
            return [
                {
                    "id": f"{config.name}-1",
                    "title": f"t-{config.name}",
                    "url": f"https://example.com/{config.name}",
                }
            ]

        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(side_effect=_fake_execute),
        ):
            runs, failed = await scrape_batch(
                sources=("first", "middle", "last"),
                max_items_per_source=10,
                session_factory=session_factory,
            )

        assert len(runs) == 2
        assert {r.source for r in runs} == {"first", "last"}
        assert len(failed) == 1
        assert failed[0]["source"] == "middle"
        assert "boom" in failed[0]["error"]


class TestScrapeExecutionError:
    async def test_runner_exception_wrapped_and_persisted(self, session_factory) -> None:
        await _seed(session_factory, _static_config())

        with (
            patch(
                "magpie.services.scrape_service._execute_static",
                new=AsyncMock(side_effect=RuntimeError("network down")),
            ),
            pytest.raises(ScrapeExecutionError),
        ):
            await scrape_once(
                source="test-static",
                max_items=5,
                session_factory=session_factory,
            )

        async with session_factory() as session:
            rows = (await session.execute(select(Run))).scalars().all()
            assert len(rows) == 1
            assert rows[0].status is RunStatus.error
            assert rows[0].error is not None


class TestSpiderClassSanity:
    def test_static_factory_returns_scrapy_class(self) -> None:
        from magpie.factory import create_scraper

        cls = create_scraper(_static_config())
        assert isinstance(cls, type) and issubclass(cls, scrapy.Spider)


class TestInvalidSourceInDb:
    async def test_corrupt_yaml_raises_unknown_source(self, session_factory) -> None:
        """If the stored YAML fails Pydantic validation, treat it as unknown."""
        async with session_factory() as session:
            session.add(
                Source(
                    name="broken",
                    origin=SourceOrigin.api,
                    config_yaml="name: broken\nurl: not-a-url\n",  # invalid url
                    config_sha="x",
                )
            )
            await session.commit()

        with pytest.raises(UnknownSourceError):
            await scrape_once(
                source="broken",
                max_items=5,
                session_factory=session_factory,
            )


class TestExecutorHelpers:
    async def test_execute_static_caps_items(self) -> None:
        import magpie.services.scrape_service as svc

        cfg = _static_config()
        with patch.object(svc, "run_spider", return_value=[{"id": str(i)} for i in range(10)]):
            out = await svc._execute_static(cfg, max_items=3)
        assert len(out) == 3

    async def test_execute_js_caps_items(self) -> None:
        import magpie.services.scrape_service as svc
        from magpie.playwright.runner import PlaywrightRunner

        cfg = _js_config()
        fake_run = AsyncMock(return_value=[{"id": str(i)} for i in range(10)])
        with patch.object(PlaywrightRunner, "run", fake_run):
            out = await svc._execute_js(cfg, max_items=4)
        assert len(out) == 4


class TestDeriveContentTextFallback:
    async def test_fallback_joins_generic_fields(self, session_factory) -> None:
        await _seed(session_factory, _static_config())
        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(
                return_value=[
                    {
                        "id": "only-id",
                        "url": "https://example.com/x",
                        "author": "Ada",
                        "quote": "Hello",
                    }
                ]
            ),
        ):
            result = await scrape_once(
                source="test-static",
                max_items=10,
                session_factory=session_factory,
            )

        assert result.items[0].content_text == "Ada\nHello"
