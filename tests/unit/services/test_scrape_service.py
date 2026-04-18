"""Service-level tests for scrape_service (spec 06-batch-scrape).

These tests drive the orchestration logic directly — config lookup,
runner dispatch, persistence, normalisation, concurrency.
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import scrapy

from magpie.config.schema import SourceConfig
from magpie.services.scrape_service import (
    ScrapeExecutionError,
    UnknownSourceError,
    scrape_batch,
    scrape_once,
)
from magpie.storage.run_repo import RunRepository


def _static_source() -> SourceConfig:
    return SourceConfig(
        name="test-static",
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


def _js_source() -> SourceConfig:
    return SourceConfig(
        name="test-js",
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


class TestRunnerDispatch:
    @pytest.mark.asyncio
    async def test_static_config_uses_scrapy_runner(self) -> None:
        static_cfg = _static_source()
        run_repo = RunRepository()

        captured: dict[str, object] = {}

        async def _fake_execute(config: SourceConfig, max_items: int) -> list[dict[str, Any]]:
            captured["dispatched_config"] = config
            captured["scraper_kind"] = "scrapy"
            return [{"id": "a", "title": "t", "url": "https://example.com/a"}]

        with (
            patch(
                "magpie.services.scrape_service._get_registered_source",
                return_value=static_cfg,
            ),
            patch(
                "magpie.services.scrape_service._execute_static",
                new=AsyncMock(side_effect=_fake_execute),
            ) as exec_static,
            patch(
                "magpie.services.scrape_service._execute_js",
                new=AsyncMock(return_value=[]),
            ) as exec_js,
        ):
            result = await scrape_once(
                source="test-static",
                max_items=5,
                run_repo=run_repo,
            )

        assert captured["scraper_kind"] == "scrapy"
        assert exec_static.await_count == 1
        assert exec_js.await_count == 0
        assert result.source == "test-static"

    @pytest.mark.asyncio
    async def test_js_config_uses_playwright_runner(self) -> None:
        js_cfg = _js_source()
        run_repo = RunRepository()

        with (
            patch(
                "magpie.services.scrape_service._get_registered_source",
                return_value=js_cfg,
            ),
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
                run_repo=run_repo,
            )

        assert exec_static.await_count == 0
        assert exec_js.await_count == 1
        assert result.source == "test-js"


class TestRunPersistence:
    @pytest.mark.asyncio
    async def test_service_writes_run_row(self) -> None:
        cfg = _static_source()
        run_repo = RunRepository()

        with (
            patch(
                "magpie.services.scrape_service._get_registered_source",
                return_value=cfg,
            ),
            patch(
                "magpie.services.scrape_service._execute_static",
                new=AsyncMock(
                    return_value=[
                        {"id": "a", "title": "t1", "url": "https://example.com/a"},
                        {"id": "b", "title": "t2", "url": "https://example.com/b"},
                    ]
                ),
            ),
        ):
            result = await scrape_once(
                source="test-static",
                max_items=10,
                run_repo=run_repo,
            )

        runs = run_repo.list_runs()
        assert len(runs) == 1
        row = runs[0]
        assert row.source == "test-static"
        assert row.status == "ok"
        assert row.item_count == 2
        assert row.duration_ms >= 0
        # run_id in response matches persisted row
        assert str(result.run_id) == row.run_id


class TestContentNormalisation:
    @pytest.mark.asyncio
    async def test_content_text_nfc_normalised_before_hash(self) -> None:
        cfg = _static_source()
        run_repo = RunRepository()

        # "é" can be expressed as U+00E9 (NFC) or "e" + U+0301 (NFD).
        nfd_text = "caf" + "e" + "\u0301"
        nfc_text = unicodedata.normalize("NFC", nfd_text)
        assert nfd_text != nfc_text  # sanity: inputs differ

        with (
            patch(
                "magpie.services.scrape_service._get_registered_source",
                return_value=cfg,
            ),
            patch(
                "magpie.services.scrape_service._execute_static",
                new=AsyncMock(
                    return_value=[
                        {
                            "id": "1",
                            "title": nfd_text,
                            "url": "https://example.com/1",
                        }
                    ]
                ),
            ),
        ):
            result = await scrape_once(
                source="test-static",
                max_items=10,
                run_repo=run_repo,
            )

        expected_hash = hashlib.sha256(nfc_text.encode("utf-8")).hexdigest()
        assert result.items[0].content_hash == expected_hash
        assert result.items[0].content_text == nfc_text


class TestEmptyItems:
    @pytest.mark.asyncio
    async def test_empty_items_returns_gracefully(self) -> None:
        cfg = _static_source()
        run_repo = RunRepository()

        with (
            patch(
                "magpie.services.scrape_service._get_registered_source",
                return_value=cfg,
            ),
            patch(
                "magpie.services.scrape_service._execute_static",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await scrape_once(
                source="test-static",
                max_items=10,
                run_repo=run_repo,
            )

        assert result.items == ()
        assert result.source == "test-static"
        rows = run_repo.list_runs()
        assert len(rows) == 1
        assert rows[0].item_count == 0
        assert rows[0].status == "ok"


class TestUnknownSource:
    @pytest.mark.asyncio
    async def test_unknown_source_raises_typed_error(self) -> None:
        run_repo = RunRepository()
        with (
            patch(
                "magpie.services.scrape_service._get_registered_source",
                side_effect=UnknownSourceError("ghost"),
            ),
            pytest.raises(UnknownSourceError),
        ):
            await scrape_once(source="ghost", max_items=5, run_repo=run_repo)


class TestBatchConcurrency:
    @pytest.mark.asyncio
    async def test_batch_isolates_one_failing_source(self) -> None:
        cfg = _static_source()
        run_repo = RunRepository()

        call_log: list[str] = []

        async def _fake_execute(config: SourceConfig, max_items: int) -> list[dict[str, Any]]:
            call_log.append(config.name)
            if config.name == "middle":
                # Yield to event loop so gather truly runs concurrently.
                await asyncio.sleep(0)
                raise RuntimeError("boom")
            return [
                {
                    "id": f"{config.name}-1",
                    "title": f"t-{config.name}",
                    "url": f"https://example.com/{config.name}",
                }
            ]

        def _lookup(name: str) -> SourceConfig:
            # Return a copy with the requested name.
            return cfg.model_copy(update={"name": name})

        with (
            patch(
                "magpie.services.scrape_service._get_registered_source",
                side_effect=_lookup,
            ),
            patch(
                "magpie.services.scrape_service._execute_static",
                new=AsyncMock(side_effect=_fake_execute),
            ),
        ):
            runs, failed = await scrape_batch(
                sources=("first", "middle", "last"),
                max_items_per_source=10,
                run_repo=run_repo,
            )

        assert len(runs) == 2
        assert {r.source for r in runs} == {"first", "last"}
        assert len(failed) == 1
        assert failed[0]["source"] == "middle"
        assert "boom" in failed[0]["error"]


class TestScrapeExecutionError:
    @pytest.mark.asyncio
    async def test_runner_exception_wrapped_in_typed_error(self) -> None:
        cfg = _static_source()
        run_repo = RunRepository()

        with (
            patch(
                "magpie.services.scrape_service._get_registered_source",
                return_value=cfg,
            ),
            patch(
                "magpie.services.scrape_service._execute_static",
                new=AsyncMock(side_effect=RuntimeError("network down")),
            ),
            pytest.raises(ScrapeExecutionError),
        ):
            await scrape_once(source="test-static", max_items=5, run_repo=run_repo)

        # Failed run is still persisted.
        runs = run_repo.list_runs()
        assert len(runs) == 1
        assert runs[0].status == "error"


class TestSpiderClassSanity:
    """Guard that the factory path we dispatch to actually returns a Scrapy class."""

    def test_static_factory_returns_scrapy_class(self) -> None:
        from magpie.factory import create_scraper

        cls = create_scraper(_static_source())
        assert isinstance(cls, type) and issubclass(cls, scrapy.Spider)


class TestConfigRegistryLookup:
    """Cover _get_registered_source branches (configs dir missing, yaml missing,
    invalid yaml)."""

    def test_missing_configs_dir_raises_unknown_source(self, tmp_path: Any) -> None:
        import magpie.services.scrape_service as svc

        with (
            patch.object(svc, "_CONFIGS_DIR", tmp_path / "does-not-exist"),
            pytest.raises(UnknownSourceError),
        ):
            svc._get_registered_source("anything")

    def test_missing_yaml_file_raises_unknown_source(self, tmp_path: Any) -> None:
        import magpie.services.scrape_service as svc

        (tmp_path / "configs").mkdir()
        with (
            patch.object(svc, "_CONFIGS_DIR", tmp_path / "configs"),
            pytest.raises(UnknownSourceError),
        ):
            svc._get_registered_source("missing")

    def test_invalid_yaml_raises_unknown_source(self, tmp_path: Any) -> None:
        import magpie.services.scrape_service as svc

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "broken.yaml").write_text("{{{not valid", encoding="utf-8")

        with (
            patch.object(svc, "_CONFIGS_DIR", configs_dir),
            pytest.raises(UnknownSourceError),
        ):
            svc._get_registered_source("broken")

    def test_valid_yaml_returns_source_config(self, tmp_path: Any) -> None:
        import magpie.services.scrape_service as svc

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "good.yaml").write_text(
            """\
name: good
url: https://example.com
schedule: 0 */6 * * *
item:
  container: tr.row
  fields:
    - {name: id, selector: "::attr(id)"}
  dedupe_key: id
""",
            encoding="utf-8",
        )

        with patch.object(svc, "_CONFIGS_DIR", configs_dir):
            cfg = svc._get_registered_source("good")
        assert cfg.name == "good"


class TestExecutorHelpers:
    """Cover the thin _execute_static / _execute_js wrappers directly."""

    @pytest.mark.asyncio
    async def test_execute_static_caps_items(self) -> None:
        import magpie.services.scrape_service as svc

        cfg = _static_source()
        with patch.object(
            svc,
            "run_spider",
            return_value=[{"id": str(i)} for i in range(10)],
        ):
            out = await svc._execute_static(cfg, max_items=3)
        assert len(out) == 3

    @pytest.mark.asyncio
    async def test_execute_js_caps_items(self) -> None:
        import magpie.services.scrape_service as svc
        from magpie.playwright.runner import PlaywrightRunner

        cfg = _js_source()
        fake_run = AsyncMock(return_value=[{"id": str(i)} for i in range(10)])
        with patch.object(PlaywrightRunner, "run", fake_run):
            out = await svc._execute_js(cfg, max_items=4)
        assert len(out) == 4


class TestDeriveContentTextFallback:
    """When no title/content/body/summary key is present, fall back to joining
    all remaining string field values."""

    @pytest.mark.asyncio
    async def test_fallback_joins_generic_fields(self) -> None:
        cfg = _static_source()
        run_repo = RunRepository()

        with (
            patch(
                "magpie.services.scrape_service._get_registered_source",
                return_value=cfg,
            ),
            patch(
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
            ),
        ):
            result = await scrape_once(source="test-static", max_items=10, run_repo=run_repo)

        # "author" + "quote" (sorted) should be used, "id"/"url" skipped.
        assert result.items[0].content_text == "Ada\nHello"
