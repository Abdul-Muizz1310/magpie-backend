"""Tests for the per-IP rate limiter middleware."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from magpie.platform.rate_limit import install_rate_limit


def _app(limit: int) -> TestClient:
    app = FastAPI()
    install_rate_limit(app, limit_per_minute=limit)

    @app.post("/api/scrape/x/once")
    async def _scrape() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/api/runs/1")
    async def _get() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def test_mutating_requests_are_limited() -> None:
    client = _app(limit=3)
    for _ in range(3):
        assert client.post("/api/scrape/x/once").status_code == 200
    resp = client.post("/api/scrape/x/once")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After")
    assert resp.json()["limit_per_minute"] == 3


def test_get_requests_are_never_limited() -> None:
    client = _app(limit=1)
    # Well past the limit, but GETs are exempt (read-only viewer/poll routes).
    for _ in range(10):
        assert client.get("/api/runs/1").status_code == 200


def test_zero_limit_disables_limiter() -> None:
    client = _app(limit=0)
    for _ in range(50):
        assert client.post("/api/scrape/x/once").status_code == 200
