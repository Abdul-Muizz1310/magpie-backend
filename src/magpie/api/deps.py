"""Shared FastAPI dependencies.

Centralising them here keeps routers slim and lets tests override the wiring
via ``app.dependency_overrides`` in one place.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from magpie.storage.db import get_session_factory


def get_session_factory_dep() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory (for services that commit themselves)."""
    return get_session_factory()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a per-request session; caller commits if they mutate."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
