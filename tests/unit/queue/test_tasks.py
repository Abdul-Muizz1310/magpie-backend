"""Tests for Procrastinate tasks using ``InMemoryConnector``.

We swap the connector so no real Postgres is required. ``get_session_factory``
is monkey-patched in the task module so the task resolves to the test's
SQLite-backed factory.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import yaml
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select

from magpie.config.schema import SourceConfig
from magpie.queue import tasks as tasks_module
from magpie.queue.app import queue_app
from magpie.queue.tasks import scrape_source_task
from magpie.storage.models import Run, RunStatus, SourceOrigin
from magpie.storage.sources_repo import SourcesRepository


def _static_config(name: str = "src") -> SourceConfig:
    return SourceConfig(
        name=name,
        url="https://example.com",  # type: ignore[arg-type]
        schedule="0 */6 * * *",
        item={  # type: ignore[arg-type]
            "container": "tr.row",
            "fields": [
                {"name": "id", "selector": "::attr(id)"},
                {"name": "title", "selector": "a::text"},
            ],
            "dedupe_key": "id",
        },
        health={"min_items": 0},  # disable underflow-heal by default
    )


@pytest.fixture
async def seeded(session_factory, monkeypatch):
    cfg = _static_config()
    async with session_factory() as session:
        await SourcesRepository(session).create(
            config=cfg,
            origin=SourceOrigin.api,
            yaml_text=yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False),
        )
        await session.commit()
    # Point the task module at our test factory.
    monkeypatch.setattr(tasks_module, "get_session_factory", lambda: session_factory)
    return session_factory


@pytest.fixture
def in_memory_app():
    connector = InMemoryConnector()
    with queue_app.replace_connector(connector) as app:
        yield app


def _items(n: int, *, base: str = "https://example.com") -> list[dict]:
    return [{"id": f"k{i}", "title": f"t{i}", "url": f"{base}/{i}"} for i in range(n)]


class TestScrapeSourceTask:
    async def test_task_runs_scrape_and_marks_ok(self, seeded, in_memory_app) -> None:
        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(return_value=_items(2)),
        ):
            summary = await scrape_source_task(source="src", max_items=10, run_id=None)
        assert summary["source"] == "src"
        assert summary["item_count"] == 2

        async with seeded() as session:
            rows = (await session.execute(select(Run))).scalars().all()
            assert len(rows) == 1
            assert rows[0].status is RunStatus.ok

    async def test_task_defers_heal_when_items_below_min(
        self, session_factory, monkeypatch, in_memory_app
    ) -> None:
        cfg = SourceConfig(
            name="underflow",
            url="https://example.com",  # type: ignore[arg-type]
            schedule="0 */6 * * *",
            item={  # type: ignore[arg-type]
                "container": "tr.row",
                "fields": [
                    {"name": "id", "selector": "::attr(id)"},
                    {"name": "title", "selector": "a::text"},
                ],
                "dedupe_key": "id",
            },
            health={"min_items": 5},
        )
        async with session_factory() as session:
            await SourcesRepository(session).create(
                config=cfg,
                origin=SourceOrigin.api,
                yaml_text=yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False),
            )
            await session.commit()
        monkeypatch.setattr(tasks_module, "get_session_factory", lambda: session_factory)

        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(return_value=_items(1)),
        ):
            await scrape_source_task(source="underflow", max_items=10, run_id=None)

        queued = [job for job in in_memory_app.connector.jobs.values()]
        queued_tasks = {job["task_name"] for job in queued}
        assert "magpie.heal_source" in queued_tasks

    async def test_sync_scrape_once_enqueues_heal_on_underflow(
        self, session_factory, monkeypatch, in_memory_app
    ) -> None:
        """The sync service path (used by /scrape, CLI, batch) also heals on underflow (MAG-1)."""
        from magpie.services.scrape_service import scrape_once

        cfg = SourceConfig(
            name="sync-underflow",
            url="https://example.com",  # type: ignore[arg-type]
            schedule="0 */6 * * *",
            item={  # type: ignore[arg-type]
                "container": "tr.row",
                "fields": [
                    {"name": "id", "selector": "::attr(id)"},
                    {"name": "title", "selector": "a::text"},
                ],
                "dedupe_key": "id",
            },
            health={"min_items": 5},
        )
        async with session_factory() as session:
            await SourcesRepository(session).create(
                config=cfg,
                origin=SourceOrigin.api,
                yaml_text=yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False),
            )
            await session.commit()

        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(return_value=_items(1)),
        ):
            await scrape_once(
                source="sync-underflow", max_items=10, session_factory=session_factory
            )

        queued_tasks = {job["task_name"] for job in in_memory_app.connector.jobs.values()}
        assert "magpie.heal_source" in queued_tasks


class TestRetryConfiguration:
    def test_scrape_task_has_retry_strategy(self) -> None:
        """The task is registered with a RetryStrategy on an httpx error set."""
        strategy = getattr(scrape_source_task, "retry_strategy", None)
        if strategy is None:
            strategy = getattr(scrape_source_task, "_retry_strategy", None)
        assert strategy is not None, "scrape_source_task must have a retry strategy"
        exceptions = set(getattr(strategy, "retry_exceptions", ()) or ())
        assert httpx.HTTPError in exceptions


class TestHealSourceTask:
    """A failed heal is logged at ERROR and returned, not raised (spec 03, 2026-10-20).

    Raising would let Procrastinate's retry repeat the LLM spend for the same outcome.
    """

    async def test_failed_heal_is_logged_at_error_and_returned(
        self, session_factory, monkeypatch, caplog
    ) -> None:
        import logging

        from magpie.healer.outcome import HealFailure, HealSummary
        from magpie.queue.tasks import heal_source_task

        monkeypatch.setattr(tasks_module, "get_session_factory", lambda: session_factory)
        failed = HealSummary(
            source="arxiv-cs",
            origin=SourceOrigin.file,
            failures=(HealFailure(target="container", reason="LLM error: 404 Not Found"),),
        )
        with (
            patch("magpie.healer.apply.heal_source", new=AsyncMock(return_value=failed)),
            caplog.at_level(logging.ERROR, logger="magpie.queue"),
        ):
            outcome = await heal_source_task(source="arxiv-cs", run_id=None)

        assert outcome["source"] == "arxiv-cs"
        assert outcome["failures"] == [
            {"target": "container", "reason": "LLM error: 404 Not Found"}
        ]
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("arxiv-cs" in m and "404" in m for m in errors), errors

    async def test_successful_heal_logs_no_error(
        self, session_factory, monkeypatch, caplog
    ) -> None:
        import logging

        from magpie.healer.outcome import HealedTarget, HealSummary
        from magpie.queue.tasks import heal_source_task
        from magpie.storage.models import HealMode

        monkeypatch.setattr(tasks_module, "get_session_factory", lambda: session_factory)
        healed = HealSummary(
            source="api-src",
            origin=SourceOrigin.api,
            healed=(
                HealedTarget(
                    target="title",
                    mode=HealMode.db_patch,
                    applied=True,
                    pr_url=None,
                    new_selector="h2::text",
                ),
            ),
        )
        with (
            patch("magpie.healer.apply.heal_source", new=AsyncMock(return_value=healed)),
            caplog.at_level(logging.ERROR, logger="magpie.queue"),
        ):
            outcome = await heal_source_task(source="api-src", run_id=None)

        assert outcome["healed"][0]["mode"] == "db_patch"
        assert outcome["failures"] == []
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]
