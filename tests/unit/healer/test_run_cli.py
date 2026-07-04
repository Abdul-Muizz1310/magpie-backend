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
        src = await repo.create(config=cfg, origin=SourceOrigin.api, yaml_text=SAMPLE_YAML)
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

    def test_main_no_failures_returns_zero(self, session_factory, monkeypatch, capsys) -> None:
        monkeypatch.setattr(heal_cli, "get_session_factory", lambda: session_factory)
        rc = heal_cli.main([])
        assert rc == 0

    def test_main_heals_underflowed_ok_run(self, session_factory, monkeypatch) -> None:
        """A latest run marked ``ok`` but below min_items is picked up for healing (MAG-1)."""
        import asyncio

        underflow_yaml = SAMPLE_YAML.replace("heal-cli-src", "underflow-src")

        async def _seed_underflow() -> None:
            cfg = SourceConfig(**yaml.safe_load(underflow_yaml))
            # Bump min_items so a 1-item ok run counts as underflow.
            cfg = cfg.model_copy(update={"health": cfg.health.model_copy(update={"min_items": 5})})
            async with session_factory() as session:
                repo = SourcesRepository(session)
                src = await repo.create(
                    config=cfg,
                    origin=SourceOrigin.api,
                    yaml_text=yaml.safe_dump(cfg.model_dump(mode="json")),
                )
                run_repo = PgRunRepository(session)
                run = await run_repo.create_queued(source_id=src.id, source_name=src.name)
                await run_repo.mark_ok(
                    run.id, item_count=1, items_new=1, items_updated=0, items_removed=0
                )
                await session.commit()

        asyncio.run(_seed_underflow())
        monkeypatch.setattr(heal_cli, "get_session_factory", lambda: session_factory)

        healed_sources: list[str] = []

        async def _fake_heal(*, source, run_id, session_factory):  # type: ignore[no-untyped-def]
            healed_sources.append(source)
            return {"source": source, "origin": "api", "healed": []}

        with patch("magpie.healer.run.heal_source", new=AsyncMock(side_effect=_fake_heal)):
            rc = heal_cli.main([])
        assert rc == 0
        assert healed_sources == ["underflow-src"]
