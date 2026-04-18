"""Tests for ``python -m magpie.healer.run``."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import yaml

import magpie.healer.run as heal_cli
from magpie.config.schema import SourceConfig
from magpie.storage.models import SourceOrigin
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository

SAMPLE_YAML = """\
name: heal-cli-src
url: https://example.com
schedule: "0 */6 * * *"
item:
  container: "div"
  fields:
    - { name: title, selector: "h2::text" }
    - { name: id, selector: "::attr(data-id)" }
  dedupe_key: id
"""


async def _seed_failed_run(session_factory) -> None:
    cfg = SourceConfig(**yaml.safe_load(SAMPLE_YAML))
    async with session_factory() as session:
        repo = SourcesRepository(session)
        src = await repo.create(
            config=cfg, origin=SourceOrigin.api, yaml_text=SAMPLE_YAML
        )
        run_repo = PgRunRepository(session)
        run = await run_repo.create_queued(source_id=src.id, source_name=src.name)
        await run_repo.mark_error(run.id, error="boom")
        await session.commit()


class TestHealCli:
    def test_main_heals_last_failed_run(self, session_factory, monkeypatch) -> None:
        import asyncio

        asyncio.run(_seed_failed_run(session_factory))
        monkeypatch.setattr(heal_cli, "get_session_factory", lambda: session_factory)

        fake_summary = {"source": "heal-cli-src", "origin": "api", "healed": []}
        with patch(
            "magpie.healer.run.heal_source",
            new=AsyncMock(return_value=fake_summary),
        ) as mock_heal:
            rc = heal_cli.main([])
        assert rc == 0
        assert mock_heal.await_count == 1

    def test_main_no_failures_returns_zero(
        self, session_factory, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(heal_cli, "get_session_factory", lambda: session_factory)
        rc = heal_cli.main([])
        assert rc == 0
