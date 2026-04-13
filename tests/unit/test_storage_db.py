"""Tests for storage.db engine, session factory, and connectivity check."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import magpie.storage.db as db_module


@pytest.fixture(autouse=True)
def _reset_engine():
    """Reset the global engine before each test."""
    db_module._engine = None
    yield
    db_module._engine = None


class TestGetEngine:
    def test_creates_engine_from_env(self) -> None:
        mock_engine = MagicMock()
        with (
            patch.dict("os.environ", {"DATABASE_URL": "sqlite:///test.db"}),
            patch("magpie.storage.db.create_async_engine", return_value=mock_engine) as mock_create,
        ):
            engine = db_module.get_engine()
            assert engine is mock_engine
            mock_create.assert_called_once()

    def test_returns_same_engine_on_second_call(self) -> None:
        mock_engine = MagicMock()
        with patch("magpie.storage.db.create_async_engine", return_value=mock_engine):
            engine1 = db_module.get_engine()
            engine2 = db_module.get_engine()
            assert engine1 is engine2

    def test_uses_default_url_when_env_missing(self) -> None:
        mock_engine = MagicMock()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("magpie.storage.db.create_async_engine", return_value=mock_engine) as mock_create,
        ):
            db_module.get_engine()
            call_args = mock_create.call_args
            assert "magpie.db" in str(call_args)


class TestGetSessionFactory:
    def test_returns_sessionmaker(self) -> None:
        mock_engine = MagicMock()
        with patch("magpie.storage.db.create_async_engine", return_value=mock_engine):
            factory = db_module.get_session_factory()
            assert factory is not None
            assert callable(factory)


class TestCheckDb:
    @pytest.mark.asyncio
    async def test_check_db_returns_true_on_success(self) -> None:
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("magpie.storage.db.AsyncSession", return_value=mock_session):
            result = await db_module.check_db()
        assert result is True

    @pytest.mark.asyncio
    async def test_check_db_returns_false_on_exception(self) -> None:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("magpie.storage.db.AsyncSession", return_value=mock_session):
            result = await db_module.check_db()
        assert result is False
