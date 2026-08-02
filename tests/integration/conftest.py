"""Testcontainers-backed Postgres fixtures for the ``slow`` integration tier.

See ``docs/specs/08-test-tiers.md``. One container is shared for the whole
session; every test gets a freshly ``CREATE DATABASE``-d database on it so
ordering can never leak state.

Fixture names are deliberately ``pg_*`` so they never shadow the root
``conftest.py``'s SQLite ``db_engine`` / ``session_factory`` — the existing fast
integration tests keep running on SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from magpie.storage.models import Base

from .docker_gate import docker_skip_reason

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import optional
    from testcontainers.community.postgres import PostgresContainer

POSTGRES_IMAGE = "postgres:16-alpine"


@dataclass(frozen=True)
class PgUrls:
    """Both driver spellings of one throwaway test database."""

    async_url: str
    """``postgresql+asyncpg://…`` — what the app's engine uses."""

    sync_url: str
    """``postgresql+psycopg://…`` — for Alembic's sync path and admin DDL."""

    plain_url: str
    """``postgresql://…`` — the stock form Neon/Render hand out, pre-normalisation."""


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    reason = docker_skip_reason()
    if reason is not None:
        pytest.skip(reason)

    # testcontainers.community is the non-deprecated home of PostgresContainer in
    # testcontainers 4.x; readiness is probed with `psql` inside the container so
    # no sync DBAPI driver is needed just to wait for boot.
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container


@pytest.fixture
def pg_database(pg_container: PostgresContainer) -> Iterator[PgUrls]:
    """Create (and afterwards drop) a per-test database on the shared container.

    Sync on purpose: Alembic's ``env.py`` calls ``asyncio.run()`` for async URLs,
    which cannot run inside pytest-asyncio's loop, so the migration tests have to
    be sync functions — and a sync test cannot consume an async fixture.
    """
    admin_url = pg_container.get_connection_url(driver="psycopg")
    base = make_url(admin_url)
    name = f"magpie_t_{uuid.uuid4().hex[:16]}"

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        target = base.set(database=name)
        yield PgUrls(
            async_url=target.set(drivername="postgresql+asyncpg").render_as_string(
                hide_password=False
            ),
            sync_url=target.render_as_string(hide_password=False),
            plain_url=target.set(drivername="postgresql").render_as_string(hide_password=False),
        )
    finally:
        with admin.connect() as conn:
            # FORCE (PG 13+) terminates leftover backends so a leaked connection
            # can't wedge teardown and cascade-fail every later test.
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
async def pg_engine(pg_database: PgUrls) -> AsyncEngine:
    """Async engine on the per-test database with the ORM schema created.

    ``NullPool`` so the concurrency test really gets two independent backend
    connections (a pooled engine could hand both tasks the same one and the
    ``FOR UPDATE`` serialisation under test would be untestable).
    """
    engine = create_async_engine(pg_database.async_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)
