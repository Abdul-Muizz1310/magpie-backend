"""FastAPI entry point for magpie."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from magpie.api.routers.jobs import router as jobs_router
from magpie.api.routers.scrape import router as scrape_router
from magpie.api.routers.sources import router as sources_router
from magpie.api.routers.viewer import router as viewer_router
from magpie.lifespan import magpie_lifespan
from magpie.platform.health import install_health_routes
from magpie.platform.metrics import install_metrics
from magpie.platform.middleware import install_cors, install_request_id
from magpie.platform.platform_token import PlatformAuthSettings, install_platform_token
from magpie.platform.query_observability import install_query_observability
from magpie.platform.rate_limit import install_rate_limit
from magpie.storage.db import get_engine

_Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
_FROM_ENV = object()


def create_app(
    *,
    auth: PlatformAuthSettings | None = None,
    rate_limit_per_minute: int | None = None,
    engine: AsyncEngine | None = None,
    database_url: str | object | None = _FROM_ENV,
    lifespan: _Lifespan = magpie_lifespan,
) -> FastAPI:
    """Build the app. Every argument defaults to the environment / the process engine.

    Middleware is installed innermost first; the request passes, outermost to
    innermost: query tagger → CORS → request id → platform token → rate limit →
    routes. The limiter sits inside the token check so it can key on the token's
    principal (spec 10), CORS sits outside it so preflights and rejections carry
    CORS headers, and slowquery-detective's tagger is outermost so every statement
    a request causes is attributed to its ``X-Request-Id`` (spec 11).
    """
    app = FastAPI(
        title="magpie",
        description="YAML-defined scrapers that self-heal",
        lifespan=lifespan,
    )
    install_health_routes(app)
    install_metrics(app)
    install_rate_limit(app, limit_per_minute=rate_limit_per_minute)
    # Fail closed: an unset DEMO_MODE is false, and with no trusted key every
    # non-public route answers 503 outside the test settings (contract v2).
    install_platform_token(app, settings=auth or PlatformAuthSettings.from_env(os.environ))
    install_request_id(app)
    install_cors(app)
    app.include_router(scrape_router)
    app.include_router(jobs_router)
    app.include_router(sources_router)
    app.include_router(viewer_router)
    # Last: install() adds the outermost middleware and wraps the lifespan.
    url = os.environ.get("DATABASE_URL") if database_url is _FROM_ENV else database_url
    install_query_observability(
        app,
        engine or get_engine(),
        url if isinstance(url, str) else None,
    )
    return app


app = create_app()

__all__ = ["app", "create_app"]
