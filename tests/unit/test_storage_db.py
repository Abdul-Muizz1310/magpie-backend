"""Tests for storage.db engine, session factory, and connectivity check."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import magpie.storage.db as db_module


@pytest.fixture(autouse=True)
def _reset_globals():
    """Reset the global engine + session factory before each test."""
    db_module._engine = None
    db_module._session_factory = None
    yield
    db_module._engine = None
    db_module._session_factory = None


class TestGetEngine:
    def test_creates_engine_from_env(self) -> None:
        mock_engine = MagicMock()
        with (
            patch.dict("os.environ", {"DATABASE_URL": "sqlite+aiosqlite:///test.db"}),
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

    def test_rewrites_postgres_scheme_to_asyncpg(self) -> None:
        mock_engine = MagicMock()
        with (
            patch.dict(
                "os.environ",
                {"DATABASE_URL": "postgresql://u:p@h/db"},
            ),
            patch("magpie.storage.db.create_async_engine", return_value=mock_engine) as mock_create,
        ):
            db_module.get_engine()
            url_arg = mock_create.call_args.args[0]
            assert url_arg == "postgresql+asyncpg://u:p@h/db"

    def test_rewrites_legacy_postgres_scheme(self) -> None:
        mock_engine = MagicMock()
        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgres://u:p@h/db"}),
            patch("magpie.storage.db.create_async_engine", return_value=mock_engine) as mock_create,
        ):
            db_module.get_engine()
            url_arg = mock_create.call_args.args[0]
            assert url_arg == "postgresql+asyncpg://u:p@h/db"

    def test_sslmode_rewritten_to_ssl_for_asyncpg(self) -> None:
        mock_engine = MagicMock()
        with (
            patch.dict(
                "os.environ",
                {"DATABASE_URL": "postgresql://u:p@h/db?sslmode=require"},
            ),
            patch("magpie.storage.db.create_async_engine", return_value=mock_engine) as mock_create,
        ):
            db_module.get_engine()
            url_arg = mock_create.call_args.args[0]
            assert url_arg == "postgresql+asyncpg://u:p@h/db?ssl=require"
            assert "sslmode" not in url_arg


class TestGetSessionFactory:
    def test_returns_callable(self) -> None:
        mock_engine = MagicMock()
        with patch("magpie.storage.db.create_async_engine", return_value=mock_engine):
            factory = db_module.get_session_factory()
            assert factory is not None
            assert callable(factory)

    def test_caches_factory(self) -> None:
        mock_engine = MagicMock()
        with patch("magpie.storage.db.create_async_engine", return_value=mock_engine):
            f1 = db_module.get_session_factory()
            f2 = db_module.get_session_factory()
            assert f1 is f2


class TestCheckDb:
    @pytest.mark.asyncio
    async def test_check_db_returns_true_on_success(self) -> None:
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_factory = MagicMock(return_value=mock_session)

        with patch("magpie.storage.db.get_session_factory", return_value=mock_factory):
            result = await db_module.check_db()
        assert result is True

    @pytest.mark.asyncio
    async def test_check_db_returns_false_on_exception(self) -> None:
        mock_session = AsyncMock()
        mock_session.__aenter__.side_effect = Exception("connection refused")
        mock_factory = MagicMock(return_value=mock_session)

        with patch("magpie.storage.db.get_session_factory", return_value=mock_factory):
            result = await db_module.check_db()
        assert result is False


class TestResetEngine:
    @pytest.mark.asyncio
    async def test_reset_disposes_cached_engine(self) -> None:
        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()
        db_module._engine = mock_engine
        db_module._session_factory = MagicMock()

        await db_module.reset_engine()
        mock_engine.dispose.assert_awaited_once()
        assert db_module._engine is None
        assert db_module._session_factory is None
