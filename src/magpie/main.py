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
from magpie.platform.middleware import install_middleware
from magpie.platform.platform_token import install_platform_token

app = FastAPI(
    title="magpie",
    description="YAML-defined scrapers that self-heal",
    lifespan=magpie_lifespan,
)
install_middleware(app)
install_health_routes(app)
install_metrics(app)
demo_mode = os.environ.get("DEMO_MODE", "true").strip().lower() not in {"0", "false", "no", ""}
install_platform_token(app, demo_mode=demo_mode)
app.include_router(scrape_router)
app.include_router(jobs_router)
app.include_router(sources_router)
app.include_router(viewer_router)

__all__ = ["app"]
