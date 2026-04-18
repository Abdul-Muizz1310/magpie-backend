"""Tests for SourcesRepository."""

from __future__ import annotations

import pytest
import yaml

from magpie.config.schema import SourceConfig
from magpie.storage.models import SourceOrigin
from magpie.storage.sources_repo import (
    DuplicateSourceError,
    ImmutableSourceError,
    SourceNotFoundError,
    SourcesRepository,
)

SAMPLE_YAML = """\
name: mysource
url: https://example.com
schedule: "0 0 * * *"
item:
  container: "div.item"
  fields:
    - { name: title, selector: "h2::text" }
  dedupe_key: title
"""


def _config(name: str = "mysource") -> tuple[SourceConfig, str]:
    text = SAMPLE_YAML.replace("mysource", name)
    return SourceConfig(**yaml.safe_load(text)), text


class TestSourcesRepository:
    async def test_create_and_get_by_name(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        await repo.create(config=cfg, origin=SourceOrigin.api, yaml_text=text)
        await db_session.commit()

        loaded = await repo.get_by_name("mysource")
        assert loaded is not None
        assert loaded.origin is SourceOrigin.api

    async def test_create_duplicate_raises(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        await repo.create(config=cfg, origin=SourceOrigin.api, yaml_text=text)
        await db_session.commit()
        with pytest.raises(DuplicateSourceError):
            await repo.create(config=cfg, origin=SourceOrigin.api, yaml_text=text)

    async def test_list_filters_by_origin(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg_a, text_a = _config("src-a")
        cfg_b, text_b = _config("src-b")
        await repo.create(config=cfg_a, origin=SourceOrigin.api, yaml_text=text_a)
        await repo.create(config=cfg_b, origin=SourceOrigin.file, yaml_text=text_b)
        await db_session.commit()

        api_only = await repo.list_all(origin=SourceOrigin.api)
        names = {s.name for s in api_only}
        assert names == {"src-a"}

        file_only = await repo.list_all(origin=SourceOrigin.file)
        names = {s.name for s in file_only}
        assert names == {"src-b"}

        all_of_them = await repo.list_all()
        assert {s.name for s in all_of_them} == {"src-a", "src-b"}

    async def test_update_config_api_origin(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        await repo.create(config=cfg, origin=SourceOrigin.api, yaml_text=text)
        await db_session.commit()

        new_text = text.replace("example.com", "updated.example.com")
        new_cfg = SourceConfig(**yaml.safe_load(new_text))
        updated = await repo.update_config(name="mysource", config=new_cfg, yaml_text=new_text)
        assert "updated.example.com" in updated.config_yaml

    async def test_update_file_origin_rejected(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        await repo.create(config=cfg, origin=SourceOrigin.file, yaml_text=text)
        await db_session.commit()

        with pytest.raises(ImmutableSourceError):
            await repo.update_config(name="mysource", config=cfg, yaml_text=text)

    async def test_update_file_origin_with_flag(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        await repo.create(config=cfg, origin=SourceOrigin.file, yaml_text=text)
        await db_session.commit()

        updated = await repo.update_config(
            name="mysource",
            config=cfg,
            yaml_text=text,
            allow_file_origin=True,
        )
        assert updated.name == "mysource"

    async def test_delete_api_origin(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        await repo.create(config=cfg, origin=SourceOrigin.api, yaml_text=text)
        await db_session.commit()
        await repo.delete(name="mysource")
        assert await repo.get_by_name("mysource") is None

    async def test_delete_file_origin_rejected(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        await repo.create(config=cfg, origin=SourceOrigin.file, yaml_text=text)
        await db_session.commit()
        with pytest.raises(ImmutableSourceError):
            await repo.delete(name="mysource")

    async def test_update_missing_raises(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        with pytest.raises(SourceNotFoundError):
            await repo.update_config(name="ghost", config=cfg, yaml_text=text)

    async def test_upsert_file_source_inserts_then_patches_on_change(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        first = await repo.upsert_file_source(config=cfg, yaml_text=text)
        original_sha = first.config_sha

        same = await repo.upsert_file_source(config=cfg, yaml_text=text)
        assert same.config_sha == original_sha

        changed_text = text.replace("example.com", "changed.example.com")
        changed_cfg = SourceConfig(**yaml.safe_load(changed_text))
        updated = await repo.upsert_file_source(config=changed_cfg, yaml_text=changed_text)
        assert updated.config_sha != original_sha

    async def test_get_config_returns_validated_source_config(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        await repo.create(config=cfg, origin=SourceOrigin.api, yaml_text=text)
        await db_session.commit()
        loaded = await repo.get_config("mysource")
        assert loaded.name == "mysource"
        assert loaded.item.dedupe_key == "title"

    async def test_get_config_missing_raises(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        with pytest.raises(SourceNotFoundError):
            await repo.get_config("ghost")


class TestListWithStats:
    """Aggregate query used by ``GET /sources``; verifies the N+1 replacement."""

    async def test_empty_db_returns_empty(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        assert await repo.list_with_stats() == []

    async def test_sources_with_no_runs_or_items_default_to_zero(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg, text = _config()
        await repo.create(config=cfg, origin=SourceOrigin.api, yaml_text=text)
        await db_session.commit()

        [stats] = await repo.list_with_stats()
        assert stats.source.name == "mysource"
        assert stats.item_count == 0
        assert stats.last_run_at is None
        assert stats.last_status is None

    async def test_aggregates_pick_latest_run_and_live_items(self, db_session) -> None:
        from magpie.storage.items_repo_pg import PgItemRepository
        from magpie.storage.models import RunStatus
        from magpie.storage.runs_repo_pg import PgRunRepository

        repo = SourcesRepository(db_session)
        cfg, text = _config("agg-source")
        source = await repo.create(config=cfg, origin=SourceOrigin.api, yaml_text=text)

        items_repo = PgItemRepository(db_session)
        await items_repo.persist_items(
            source.id,
            [{"id": "1", "title": "a"}, {"id": "2", "title": "b"}],
            dedupe_key="id",
        )
        # Mark item 2 as removed to prove item_count excludes it.
        await items_repo.persist_items(source.id, [{"id": "1", "title": "a"}], dedupe_key="id")

        runs_repo = PgRunRepository(db_session)
        r1 = await runs_repo.create_queued(source_id=source.id, source_name=source.name)
        await runs_repo.mark_ok(r1.id, item_count=2, items_new=2, items_updated=0, items_removed=0)
        # Nudge the clock so r2's started_at is strictly later than r1's —
        # otherwise two ``datetime.now(UTC)`` calls can collide on a fast
        # machine and the ROW_NUMBER() tie-break is undefined.
        import asyncio

        await asyncio.sleep(0.01)
        r2 = await runs_repo.create_queued(source_id=source.id, source_name=source.name)
        await runs_repo.mark_error(r2.id, error="boom")
        await db_session.commit()

        [stats] = await repo.list_with_stats()
        assert stats.source.name == "agg-source"
        assert stats.item_count == 1  # id=2 was marked removed
        assert stats.last_run_at is not None
        assert stats.last_status is RunStatus.error  # latest of the two runs

    async def test_list_with_stats_filters_by_origin(self, db_session) -> None:
        repo = SourcesRepository(db_session)
        cfg_a, text_a = _config("src-api")
        cfg_b, text_b = _config("src-file")
        await repo.create(config=cfg_a, origin=SourceOrigin.api, yaml_text=text_a)
        await repo.create(config=cfg_b, origin=SourceOrigin.file, yaml_text=text_b)
        await db_session.commit()

        api_only = await repo.list_with_stats(origin=SourceOrigin.api)
        assert {s.source.name for s in api_only} == {"src-api"}
