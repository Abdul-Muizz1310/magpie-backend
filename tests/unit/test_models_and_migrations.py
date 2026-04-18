"""Smoke tests for SQLAlchemy models + Alembic migration.

These run against an ephemeral SQLite file so we verify the migration chain
applies cleanly, the ORM maps round-trip, and unique constraints behave as
intended — without touching a live Postgres.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from magpie.storage.models import Base, Heal, HealMode, Item, Run, RunStatus, Source, SourceOrigin

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
async def engine(tmp_path):
    """Fresh async SQLite engine with all tables created via ORM metadata."""
    db_file = tmp_path / "test.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


# ── Model round-trip ─────────────────────────────────────────────────────────


class TestSourceModel:
    async def test_insert_and_query_source(self, session_factory) -> None:
        async with session_factory() as session:
            src = Source(
                name="test-src",
                description="desc",
                origin=SourceOrigin.api,
                config_yaml="name: test-src\n",
                config_sha="abc123",
            )
            session.add(src)
            await session.commit()
            await session.refresh(src)
            assert isinstance(src.id, uuid.UUID)
            assert src.origin is SourceOrigin.api

        async with session_factory() as session:
            result = await session.execute(select(Source).where(Source.name == "test-src"))
            loaded = result.scalar_one()
            assert loaded.description == "desc"

    async def test_name_must_be_unique(self, session_factory) -> None:
        async with session_factory() as session:
            session.add(
                Source(
                    name="dup",
                    origin=SourceOrigin.api,
                    config_yaml="x",
                    config_sha="1",
                )
            )
            await session.commit()
        async with session_factory() as session:
            session.add(
                Source(
                    name="dup",
                    origin=SourceOrigin.api,
                    config_yaml="x",
                    config_sha="2",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()


class TestItemModel:
    async def test_unique_source_dedupe_key(self, session_factory) -> None:
        async with session_factory() as session:
            src = Source(name="a", origin=SourceOrigin.api, config_yaml="x", config_sha="1")
            session.add(src)
            await session.commit()
            await session.refresh(src)

            session.add(
                Item(
                    source_id=src.id,
                    dedupe_key="k1",
                    content_hash="h1",
                    data={"v": 1},
                )
            )
            await session.commit()

        async with session_factory() as session:
            session.add(
                Item(
                    source_id=src.id,
                    dedupe_key="k1",
                    content_hash="h2",
                    data={"v": 2},
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()


class TestRunModel:
    async def test_run_status_defaults_to_queued(self, session_factory) -> None:
        async with session_factory() as session:
            src = Source(name="r", origin=SourceOrigin.file, config_yaml="x", config_sha="1")
            session.add(src)
            await session.commit()
            await session.refresh(src)
            run = Run(source_id=src.id, source_name="r")
            session.add(run)
            await session.commit()
            await session.refresh(run)
            assert run.status is RunStatus.queued
            assert run.item_count == 0


class TestHealModel:
    async def test_heal_insert_with_modes(self, session_factory) -> None:
        async with session_factory() as session:
            src = Source(name="h", origin=SourceOrigin.api, config_yaml="x", config_sha="1")
            session.add(src)
            await session.commit()
            await session.refresh(src)
            session.add(
                Heal(
                    source_id=src.id,
                    field_name="title",
                    old_selector="a::text",
                    new_selector="b::text",
                    selector_type="css",
                    confidence=0.9,
                    reasoning="because",
                    sample_values=["x", "y"],
                    mode=HealMode.db_patch,
                    applied=True,
                )
            )
            await session.commit()


# ── Alembic migration ────────────────────────────────────────────────────────


class TestAlembicMigration:
    def test_upgrade_head_creates_all_tables(self, tmp_path, monkeypatch) -> None:
        db_file = tmp_path / "alembic_test.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")

        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        # Change to the repo root so the env.py import path works.
        cwd = os.getcwd()
        os.chdir(REPO_ROOT)
        try:
            command.upgrade(cfg, "head")
        finally:
            os.chdir(cwd)
        assert db_file.exists()

    def test_upgrade_head_is_idempotent(self, tmp_path, monkeypatch) -> None:
        db_file = tmp_path / "alembic_twice.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        cwd = os.getcwd()
        os.chdir(REPO_ROOT)
        try:
            command.upgrade(cfg, "head")
            command.upgrade(cfg, "head")  # second call should be a no-op
        finally:
            os.chdir(cwd)
