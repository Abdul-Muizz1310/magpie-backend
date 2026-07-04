"""Tests for the magpie CLI."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import yaml

import magpie.cli as cli_module
from magpie.config.schema import SourceConfig
from magpie.storage.models import SourceOrigin
from magpie.storage.sources_repo import SourcesRepository

SAMPLE_YAML = """\
name: cli-src
url: https://example.com
schedule: "0 */6 * * *"
item:
  container: "tr.row"
  fields:
    - { name: id, selector: "::attr(id)" }
    - { name: title, selector: "a::text" }
  dedupe_key: id
"""


async def _seed(session_factory, origin: SourceOrigin = SourceOrigin.api) -> None:
    cfg = SourceConfig(**yaml.safe_load(SAMPLE_YAML))
    async with session_factory() as session:
        await SourcesRepository(session).create(config=cfg, origin=origin, yaml_text=SAMPLE_YAML)
        await session.commit()


@pytest.fixture(autouse=True)
def _bind_session_factory(session_factory, monkeypatch):
    monkeypatch.setattr(cli_module, "get_session_factory", lambda: session_factory)
    # Also bind the lifespan-sync helper's factory so CLI's startup-sync works.
    import magpie.lifespan as lifespan_module

    monkeypatch.setattr(lifespan_module, "get_session_factory", lambda: session_factory)


@pytest.fixture
def empty_configs(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    import magpie.lifespan as lifespan_module

    monkeypatch.setattr(cli_module, "configs_dir", lambda: cfg_dir)
    monkeypatch.setattr(lifespan_module, "configs_dir", lambda: cfg_dir)
    return cfg_dir


class TestRunCommand:
    async def test_run_known_source_returns_zero(self, session_factory) -> None:
        await _seed(session_factory)
        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(return_value=[{"id": "a", "title": "t", "url": "https://example.com/a"}]),
        ):
            rc = await cli_module._run_one("cli-src", max_items=5)
        assert rc == 0

    async def test_run_unknown_source_returns_nonzero(self, session_factory) -> None:
        rc = await cli_module._run_one("ghost", max_items=5)
        assert rc == 2

    async def test_run_underflow_returns_exit_underflow(self, session_factory) -> None:
        """A near-empty scrape (< min_items) exits non-zero so CI escalates to heal (MAG-1)."""
        await _seed(session_factory)  # default health.min_items == 1
        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(return_value=[]),
        ):
            rc = await cli_module._run_one("cli-src", max_items=5)
        assert rc == cli_module.EXIT_UNDERFLOW

    async def test_run_all_with_no_configs(self, empty_configs) -> None:
        rc = await cli_module._run_all(max_items=5)
        assert rc == 0


class TestSyncCommand:
    async def test_sync_inserts_file_sources(self, empty_configs, session_factory) -> None:
        (empty_configs / "hn.yaml").write_text(
            SAMPLE_YAML.replace("cli-src", "hn"), encoding="utf-8"
        )
        rc = await cli_module._sync()
        assert rc == 0
        async with session_factory() as session:
            row = await SourcesRepository(session).get_by_name("hn")
            assert row is not None
            assert row.origin is SourceOrigin.file


class TestMigrateCommand:
    def test_migrate_runs_alembic(self, tmp_path, monkeypatch) -> None:
        db_file = tmp_path / "m.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
        # Reset the cached engine so the new URL is picked up.
        import magpie.storage.db as db_module

        db_module._engine = None
        db_module._session_factory = None
        rc = cli_module._migrate()
        assert rc == 0
        assert db_file.exists()


class TestMainDispatch:
    def test_main_missing_subcommand_returns_2(self, monkeypatch, capsys) -> None:
        with pytest.raises(SystemExit) as exc_info:
            cli_module.main([])
        # argparse with required=True exits with code 2
        assert exc_info.value.code == 2

    # ``cli.main`` calls ``asyncio.run`` internally, so these tests must not
    # themselves run inside an event loop. Using plain ``def`` (not ``async
    # def``) keeps pytest-asyncio from wrapping them.

    def test_main_run_dispatches_to_run_one(self, session_factory) -> None:
        import asyncio

        asyncio.run(_seed(session_factory))
        with patch(
            "magpie.services.scrape_service._execute_static",
            new=AsyncMock(return_value=[{"id": "a", "title": "t", "url": "https://example.com/a"}]),
        ):
            rc = cli_module.main(["run", "cli-src", "--max-items", "5"])
        assert rc == 0

    def test_main_sync_dispatches(self, empty_configs) -> None:
        (empty_configs / "src.yaml").write_text(
            SAMPLE_YAML.replace("cli-src", "sync-test"), encoding="utf-8"
        )
        rc = cli_module.main(["sync"])
        assert rc == 0


class TestDueCommand:
    def test_due_all_emits_every_source(self, empty_configs, capsys) -> None:
        import json

        (empty_configs / "a.yaml").write_text(
            SAMPLE_YAML.replace("cli-src", "src-a"), encoding="utf-8"
        )
        (empty_configs / "b.yaml").write_text(
            SAMPLE_YAML.replace("cli-src", "src-b"), encoding="utf-8"
        )
        rc = cli_module.main(["due", "--all"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert set(payload["sources"]) == {"src-a", "src-b"}

    def test_due_filters_by_schedule(self, empty_configs, capsys, monkeypatch) -> None:
        import json
        from datetime import UTC, datetime

        # A source scheduled weekly on Sunday; "now" is a Saturday -> not due.
        weekly = SAMPLE_YAML.replace("cli-src", "weekly-src").replace(
            'schedule: "0 */6 * * *"', 'schedule: "0 0 * * 0"'
        )
        (empty_configs / "weekly.yaml").write_text(weekly, encoding="utf-8")

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return datetime(2026, 7, 4, 12, 30, tzinfo=UTC)

        monkeypatch.setattr(cli_module, "datetime", _FrozenDatetime)
        rc = cli_module.main(["due", "--window-seconds", "3600"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["sources"] == []
