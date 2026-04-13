"""Middleware — X-Request-Id propagation and CORS."""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

_Handler = Callable[[Request], Awaitable[Response]]

_PROD_ORIGINS = [
    "https://magpie-frontend.vercel.app",
    "https://bastion-six.vercel.app",
]


def _get_allowed_origins() -> list[str]:
    origins = list(_PROD_ORIGINS)
    if os.environ.get("APP_ENV", "development") != "production":
        origins.append("http://localhost:3000")
    return origins


def install_middleware(app: FastAPI) -> None:
    """Attach CORS and request-id middleware to ``app``."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_get_allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next: _Handler) -> Response:
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response
