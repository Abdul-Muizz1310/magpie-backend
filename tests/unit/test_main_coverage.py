"""Residual main.py coverage tests.

After the DB rewire, ``main.py`` is a thin router-registration shim. The old
``_load_configs`` + demo-data helpers are gone (now in the ``viewer`` router
and ``lifespan._sync_file_sources_to_db``). Only the /health DB-down paths
are still worth covering here.
"""

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


class TestHealthDbDown:
    async def test_health_db_down(self, client: AsyncClient) -> None:
        with patch(
            "magpie.storage.db.check_db",
            new_callable=AsyncMock,
            return_value=False,
        ):
            resp = await client.get("/health")
            assert resp.status_code == 503
            body = resp.json()
            assert body["db"] == "down"
            assert body["status"] == "degraded"

    async def test_health_db_exception(self, client: AsyncClient) -> None:
        with patch(
            "magpie.storage.db.check_db",
            new_callable=AsyncMock,
            side_effect=Exception("connection error"),
        ):
            resp = await client.get("/health")
            assert resp.status_code == 503
            assert resp.json()["db"] == "down"

    async def test_health_db_ok_returns_200(self, client: AsyncClient) -> None:
        with patch(
            "magpie.storage.db.check_db",
            new_callable=AsyncMock,
            return_value=True,
        ):
            resp = await client.get("/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert body["db"] == "ok"
