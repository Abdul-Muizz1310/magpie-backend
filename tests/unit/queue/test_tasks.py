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


class TestRetryConfiguration:
    def test_scrape_task_has_retry_strategy(self) -> None:
        """The task is registered with a RetryStrategy on an httpx error set."""
        strategy = getattr(scrape_source_task, "retry_strategy", None)
        if strategy is None:
            strategy = getattr(scrape_source_task, "_retry_strategy", None)
        assert strategy is not None, "scrape_source_task must have a retry strategy"
        exceptions = set(getattr(strategy, "retry_exceptions", ()) or ())
        assert httpx.HTTPError in exceptions
