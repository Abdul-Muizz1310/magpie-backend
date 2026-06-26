"""Prometheus metrics — exposes ``/metrics`` for scraping.

Uses ``prometheus-fastapi-instrumentator`` to record default HTTP request
metrics (latency, request/response sizes, counts) and serve them in the
Prometheus text exposition format. The endpoint is hidden from the OpenAPI
schema to keep it out of the public API surface.
"""

from __future__ import annotations

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator


def install_metrics(app: FastAPI) -> None:
    """Attach the Prometheus instrumentator and /metrics endpoint to ``app``."""
    Instrumentator().instrument(app).expose(app, include_in_schema=False)
