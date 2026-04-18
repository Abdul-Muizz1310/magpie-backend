"""Platform endpoints — /health and /version.

``/health`` returns **503** when the database is unreachable so Render (or any
container orchestrator) can take the instance out of rotation / restart it.
Returning 200 with ``"db": "down"`` — the previous behaviour — made DB outages
invisible to the platform.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

SERVICE_NAME = "magpie"


def install_health_routes(app: FastAPI) -> None:
    """Attach /health and /version endpoints to ``app``."""

    @app.get("/health", include_in_schema=False)
    async def _health() -> JSONResponse:
        db_ok = False
        try:
            from magpie.storage.db import check_db

            db_ok = await check_db()
        except Exception:
            db_ok = False

        status_code = 200 if db_ok else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ok" if db_ok else "degraded",
                "service": SERVICE_NAME,
                "version": os.environ.get("COMMIT_SHA", "dev"),
                "db": "ok" if db_ok else "down",
            },
        )

    @app.get("/version", include_in_schema=False)
    async def _version() -> JSONResponse:
        return JSONResponse(
            {"service": SERVICE_NAME, "version": os.environ.get("COMMIT_SHA", "dev")}
        )
