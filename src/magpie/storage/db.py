"""Database engine and session management.

The URL is read from ``DATABASE_URL``. Postgres URLs may come in either
``postgres://`` or ``postgresql://`` form (Neon / Render both emit the latter);
we rewrite both to ``postgresql+asyncpg://`` so SQLAlchemy picks the async
driver. Any other scheme is left untouched — tests use ``sqlite+aiosqlite://``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _normalize_url(url: str) -> str:
    """Rewrite Postgres URLs so SQLAlchemy uses the async asyncpg driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _database_url() -> str:
    return _normalize_url(os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///magpie.db"))


def get_engine() -> AsyncEngine:
    """Get or create the process-wide async SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(_database_url(), echo=False, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get the process-wide async session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def reset_engine() -> None:
    """Dispose of the cached engine (used by tests and graceful shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def session_dependency() -> AsyncIterator[AsyncSession]:
    """FastAPI ``Depends()`` generator yielding a session per request."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def check_db() -> bool:
    """Check database connectivity."""
    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
