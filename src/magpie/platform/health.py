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

UNKNOWN_SHA = "unknown"
"""What ``/version`` reports when no build or platform ever told us a real SHA."""

PLACEHOLDER_SHAS = frozenset({UNKNOWN_SHA, "dev", "none", "null", "latest"})
"""Values that are *set* but carry no information, so resolution must look past them.

``Dockerfile`` declares ``ARG COMMIT_SHA=unknown`` and promotes it to ``ENV``, so
an image built without ``--build-arg COMMIT_SHA=...`` has the variable literally
set to ``"unknown"``. A plain ``os.environ.get("COMMIT_SHA", default)`` can never
fall through that, which is why the deployed service reported a placeholder even
though Render exposes the real SHA in ``RENDER_GIT_COMMIT``.
"""

_SHA_ENV_VARS = ("COMMIT_SHA", "RENDER_GIT_COMMIT", "GITHUB_SHA")
"""Resolution order: explicit build arg → Render's runtime value → CI's value."""


def commit_sha() -> str:
    """Best available commit SHA for this running image.

    Tries each source in ``_SHA_ENV_VARS`` order and skips anything blank or in
    ``PLACEHOLDER_SHAS``, so a not-really-set value never shadows a real one.
    """
    for name in _SHA_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value and value.lower() not in PLACEHOLDER_SHAS:
            return value
    return UNKNOWN_SHA


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

        sha = commit_sha()
        status_code = 200 if db_ok else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ok" if db_ok else "degraded",
                "service": SERVICE_NAME,
                "version": sha,
                "commit_sha": sha,
                "db": "ok" if db_ok else "down",
            },
        )

    @app.get("/version", include_in_schema=False)
    async def _version() -> JSONResponse:
        sha = commit_sha()
        return JSONResponse({"service": SERVICE_NAME, "version": sha, "commit_sha": sha})
