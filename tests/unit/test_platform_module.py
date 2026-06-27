"""Tests for platform/ module — middleware, health, logging."""

from __future__ import annotations

import logging

from httpx import ASGITransport, AsyncClient

from magpie.main import app
from magpie.platform.logging import configure_logging


async def test_health_endpoint() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "magpie"
    assert "commit_sha" in body
    assert body["commit_sha"] == body["version"]


async def test_version_endpoint() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert "version" in body
    assert "commit_sha" in body
    assert body["commit_sha"] == body["version"]


async def test_metrics_endpoint() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert "# HELP" in resp.text


async def test_request_id_header() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health", headers={"x-request-id": "test-123"})
    assert resp.headers.get("x-request-id") == "test-123"


async def test_request_id_generated_if_absent() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.headers.get("x-request-id")


def _active_log_format() -> str:
    """Return the format string of the root logger's active handler."""
    handler = logging.getLogger().handlers[0]
    assert handler.formatter is not None
    return handler.formatter._fmt or ""


def test_configure_logging_dev(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("APP_ENV", raising=False)
    configure_logging()
    root = logging.getLogger()
    assert root.level == logging.INFO
    # Dev uses the human-readable format, not JSON.
    assert "[magpie]" in _active_log_format()


def test_configure_logging_prod(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The stack sets APP_ENV in production (render.yaml / Dockerfile), so JSON
    # logging must key on APP_ENV — a regression test for the ENVIRONMENT bug.
    monkeypatch.setenv("APP_ENV", "production")
    configure_logging()
    root = logging.getLogger()
    assert root.level == logging.INFO
    fmt = _active_log_format()
    assert fmt.startswith("{") and '"service":"magpie"' in fmt


def test_configure_logging_ignores_legacy_environment_var(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """ENVIRONMENT=production alone must NOT enable JSON — only APP_ENV does."""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    configure_logging()
    assert "[magpie]" in _active_log_format()
