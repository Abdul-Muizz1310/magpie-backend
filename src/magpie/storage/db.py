"""Database engine and session management."""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """Get or create the async SQLAlchemy engine."""
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///magpie.db")
        _engine = create_async_engine(url, echo=False)
    return _engine


def get_session_factory() -> sessionmaker:
    """Get a sessionmaker bound to the engine."""
    return sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)


async def check_db() -> bool:
    """Check database connectivity."""
    try:
        async with AsyncSession(get_engine()) as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
