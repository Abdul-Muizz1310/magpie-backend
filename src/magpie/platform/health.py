"""Platform endpoints — /health and /version."""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

SERVICE_NAME = "magpie"


def install_health_routes(app: FastAPI) -> None:
    """Attach /health and /version endpoints to ``app``."""

    @app.get("/health", include_in_schema=False)
    async def _health() -> JSONResponse:
        db_status = "ok"
        try:
            from magpie.storage.db import check_db

            if not await check_db():
                db_status = "down"
        except Exception:
            db_status = "down"

        return JSONResponse(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "version": os.environ.get("COMMIT_SHA", "dev"),
                "db": db_status,
            }
        )

    @app.get("/version", include_in_schema=False)
    async def _version() -> JSONResponse:
        return JSONResponse(
            {"service": SERVICE_NAME, "version": os.environ.get("COMMIT_SHA", "dev")}
        )
