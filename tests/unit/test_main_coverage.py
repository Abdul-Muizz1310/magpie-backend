"""Tests for uncovered branches in main.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from magpie.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestLoadConfigsBranches:
    """Cover lines 82, 89, 98-99 in _load_configs by patching the configs dir path."""

    def test_no_configs_dir_returns_empty(self, tmp_path) -> None:
        """When configs_dir doesn't exist, return empty list (line 82)."""
        import magpie.main as main_module

        fake_path = tmp_path / "src" / "magpie" / "main.py"
        fake_path.parent.mkdir(parents=True, exist_ok=True)
        fake_path.touch()
        # no configs dir => should return []

        with patch.object(main_module, "__file__", str(fake_path)):
            result = main_module._load_configs()
        assert result == []

    def test_yaml_non_dict_skipped(self, tmp_path) -> None:
        """YAML that parses to non-dict or missing 'name' is skipped (line 89)."""
        import magpie.main as main_module

        # Set up directory: tmp_path/src/magpie/main.py  => configs at tmp_path/configs
        fake_main = tmp_path / "src" / "magpie" / "main.py"
        fake_main.parent.mkdir(parents=True, exist_ok=True)
        fake_main.touch()

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "list.yaml").write_text("- item1\n- item2\n", encoding="utf-8")
        (configs_dir / "noname.yaml").write_text("description: no name key\n", encoding="utf-8")
        (configs_dir / "good.yaml").write_text(
            "name: test-good\ndescription: good\n", encoding="utf-8"
        )

        with patch.object(main_module, "__file__", str(fake_main)):
            result = main_module._load_configs()

        assert len(result) == 1
        assert result[0]["name"] == "test-good"

    def test_corrupt_yaml_exception_skipped(self, tmp_path) -> None:
        """Corrupt YAML triggers except/continue branch (lines 98-99)."""
        import magpie.main as main_module

        fake_main = tmp_path / "src" / "magpie" / "main.py"
        fake_main.parent.mkdir(parents=True, exist_ok=True)
        fake_main.touch()

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "bad.yaml").write_text("{{{invalid", encoding="utf-8")
        (configs_dir / "ok.yaml").write_text("name: test-ok\n", encoding="utf-8")

        with patch.object(main_module, "__file__", str(fake_main)):
            result = main_module._load_configs()

        assert len(result) == 1
        assert result[0]["name"] == "test-ok"


class TestHealthDbDown:
    @pytest.mark.asyncio
    async def test_health_db_down(self, client: AsyncClient) -> None:
        """Cover lines 270-271: check_db returns False."""
        with patch(
            "magpie.storage.db.check_db",
            new_callable=AsyncMock,
            return_value=False,
        ):
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["db"] == "down"

    @pytest.mark.asyncio
    async def test_health_db_exception(self, client: AsyncClient) -> None:
        """Cover lines 270-271: check_db raises exception."""
        with patch(
            "magpie.storage.db.check_db",
            new_callable=AsyncMock,
            side_effect=Exception("connection error"),
        ):
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["db"] == "down"


