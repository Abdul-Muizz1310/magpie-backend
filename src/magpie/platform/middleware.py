"""Middleware — X-Request-Id propagation and CORS (order is set in ``magpie.main``)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

_Handler = Callable[[Request], Awaitable[Response]]

_PROD_ORIGINS = [
    "https://magpie-frontend-three.vercel.app",
    "https://bastion-six.vercel.app",
]


def _get_allowed_origins() -> list[str]:
    """Compose the CORS origin list.

    Baseline is the two hardcoded production frontends. ``FRONTEND_URL`` is
    read at startup and appended if set — that lets a preview deploy CORS
    through without code changes. Local dev (``APP_ENV != production``) also
    gets ``http://localhost:3000``.
    """
    origins = list(_PROD_ORIGINS)
    extra = os.environ.get("FRONTEND_URL", "").strip()
    if extra and extra not in origins:
        origins.append(extra)
    if os.environ.get("APP_ENV", "development") != "production":
        origins.append("http://localhost:3000")
    return origins


# Contract v2 (spec 10): the browser may send the platform token and a request id.
ALLOWED_REQUEST_HEADERS = ["content-type", "x-platform-token", "x-request-id", "authorization"]


def install_cors(app: FastAPI) -> None:
    """Attach CORS. Install it last so it is the outermost middleware.

    Outermost means a browser preflight is answered before the token check (which
    would 401 it), and a 401/503 from that check still carries
    ``Access-Control-Allow-Origin`` so the page can read the error.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_get_allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=ALLOWED_REQUEST_HEADERS,
        expose_headers=["x-request-id"],
    )


def install_request_id(app: FastAPI) -> None:
    """Echo ``X-Request-Id`` (or a fresh UUID) on every response, rejections included."""

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next: _Handler) -> Response:
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response
