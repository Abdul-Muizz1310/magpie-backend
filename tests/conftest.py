"""Shared fixtures for magpie tests.

``db_session`` provides a per-test AsyncSession bound to an ephemeral SQLite
file whose schema is built from ``Base.metadata`` (no Alembic round-trip per
test). Tests that want the full Alembic upgrade path should use the dedicated
fixtures in ``tests/unit/test_models_and_migrations.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from magpie.storage.models import Base


@pytest.fixture
async def db_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    db_file = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def db_session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
