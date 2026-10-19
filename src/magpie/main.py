"""FastAPI entry point for magpie."""

from __future__ import annotations

import os

from fastapi import FastAPI

from magpie.api.routers.jobs import router as jobs_router
from magpie.api.routers.scrape import router as scrape_router
from magpie.api.routers.sources import router as sources_router
from magpie.api.routers.viewer import router as viewer_router
from magpie.lifespan import magpie_lifespan
from magpie.platform.health import install_health_routes
from magpie.platform.metrics import install_metrics
from magpie.platform.middleware import install_cors, install_request_id
from magpie.platform.platform_token import PlatformAuthSettings, install_platform_token
from magpie.platform.rate_limit import install_rate_limit


def create_app(
    *,
    auth: PlatformAuthSettings | None = None,
    rate_limit_per_minute: int | None = None,
) -> FastAPI:
    """Build the app. ``auth``/``rate_limit_per_minute`` default to the environment.

    Middleware is installed innermost first; the request passes, outermost to
    innermost: CORS → request id → platform token → rate limit → routes. The
    limiter sits inside the token check so it can key on the token's
    ``iss:sub`` (spec 10), and CORS sits outside it so preflights and rejections
    carry CORS headers.
    """
    app = FastAPI(
        title="magpie",
        description="YAML-defined scrapers that self-heal",
        lifespan=magpie_lifespan,
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
    return app


app = create_app()

__all__ = ["app", "create_app"]
