"""Tests for the lifespan helpers.

The full ``magpie_lifespan`` context manager requires real Procrastinate +
Postgres wiring, so we test the pieces in isolation: the source-sync function
here, and the worker task indirectly via the queue-task suite.
"""

from __future__ import annotations

import pytest

import magpie.lifespan as lifespan_module
from magpie.storage.models import SourceOrigin
from magpie.storage.sources_repo import SourcesRepository


@pytest.fixture
def isolated_configs(tmp_path, monkeypatch):
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr(lifespan_module, "configs_dir", lambda: configs_dir)
    return configs_dir


@pytest.fixture(autouse=True)
def _bind_session_factory(session_factory, monkeypatch):
    monkeypatch.setattr(lifespan_module, "get_session_factory", lambda: session_factory)


class TestSyncFileSources:
    async def test_sync_skips_if_dir_missing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(lifespan_module, "configs_dir", lambda: tmp_path / "missing")
        count = await lifespan_module._sync_file_sources_to_db()
        assert count == 0

    async def test_sync_inserts_file_origin_rows(
        self, isolated_configs, session_factory
    ) -> None:
        (isolated_configs / "hacker.yaml").write_text(
            """\
name: hacker
url: https://news.ycombinator.com
schedule: "0 */6 * * *"
item:
  container: "tr.athing"
  fields:
    - { name: id, selector: "::attr(id)" }
    - { name: title, selector: "a::text" }
  dedupe_key: id
""",
            encoding="utf-8",
        )
        count = await lifespan_module._sync_file_sources_to_db()
        assert count == 1

        async with session_factory() as session:
            src = await SourcesRepository(session).get_by_name("hacker")
            assert src is not None
            assert src.origin is SourceOrigin.file

    async def test_sync_is_idempotent(self, isolated_configs, session_factory) -> None:
        (isolated_configs / "hacker.yaml").write_text(
            """\
name: hacker
url: https://news.ycombinator.com
schedule: "0 */6 * * *"
item:
  container: "tr.athing"
  fields:
    - { name: id, selector: "::attr(id)" }
    - { name: title, selector: "a::text" }
  dedupe_key: id
""",
            encoding="utf-8",
        )
        await lifespan_module._sync_file_sources_to_db()
        await lifespan_module._sync_file_sources_to_db()
        async with session_factory() as session:
            sources = await SourcesRepository(session).list_all()
            assert len(sources) == 1

    async def test_sync_skips_invalid_yaml(
        self, isolated_configs, session_factory
    ) -> None:
        (isolated_configs / "bad.yaml").write_text("{{ not valid", encoding="utf-8")
        (isolated_configs / "good.yaml").write_text(
            """\
name: good
url: https://example.com
schedule: "0 */6 * * *"
item:
  container: "div.item"
  fields:
    - { name: title, selector: "h2::text" }
  dedupe_key: title
""",
            encoding="utf-8",
        )
        count = await lifespan_module._sync_file_sources_to_db()
        assert count == 1
        async with session_factory() as session:
            names = {s.name for s in await SourcesRepository(session).list_all()}
            assert names == {"good"}
