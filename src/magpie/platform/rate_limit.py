"""Per-caller rate limiting for mutating / scrape / enqueue routes.

The API triggers real outbound network fetches and paid LLM/GitHub spend, so an
unauthenticated (demo-mode) or even authenticated caller must not be able to
hammer the mutating endpoints without bound. This is an in-process, per-client
fixed-window limiter applied to every non-safe HTTP method (POST/PUT/PATCH/
DELETE) — which covers source create/update/delete, ``/scrape/*/once``,
``/scrape/batch`` and ``/scrape/*/enqueue``. Read-only GETs (the viewer/poll
routes) are never throttled.

It intentionally runs regardless of ``DEMO_MODE`` — it's an abuse ceiling, not
authentication. ``RATE_LIMIT_PER_MINUTE <= 0`` disables it (documented escape
hatch for trusted single-tenant deploys).

Buckets (contract v2 as amended 2026-10-20, spec 10): a bastion token keys on
``"bastion:{sub}"``; a magpie-frontend token on ``"magpie-frontend:{address}"``
(its ``sub`` is constant); anything else on the address. The address is the
leftmost ``X-Forwarded-For`` entry, which uvicorn resolves behind Render. The
limiter must run inside the token middleware so the principal is known.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from magpie.platform.principal import principal_of, rate_limit_key

_Handler = Callable[[Request], Awaitable[Response]]

_WINDOW_S = 60.0
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Bound the in-memory bucket map so a spray of distinct source IPs can't grow it
# without limit; stale windows are pruned first, then oldest entries dropped.
_MAX_BUCKETS = 10_000


def _limit_from_env() -> int:
    raw = os.environ.get("RATE_LIMIT_PER_MINUTE", "30").strip()
    try:
        return int(raw)
    except ValueError:
        return 30


def bucket_key(request: Request) -> str:
    """The request's bucket: see :func:`magpie.platform.principal.rate_limit_key`."""
    address = request.client.host if request.client else "unknown"
    return rate_limit_key(principal_of(request), address)


def install_rate_limit(app: FastAPI, *, limit_per_minute: int | None = None) -> None:
    """Attach the per-IP fixed-window rate limiter to ``app``."""
    limit = _limit_from_env() if limit_per_minute is None else limit_per_minute
    buckets: dict[str, tuple[float, int]] = {}
    lock = asyncio.Lock()

    @app.middleware("http")
    async def _rate_limit_middleware(request: Request, call_next: _Handler) -> Response:
        if limit <= 0 or request.method in _SAFE_METHODS:
            return await call_next(request)

        key = bucket_key(request)
        now = time.monotonic()

        async with lock:
            if len(buckets) >= _MAX_BUCKETS:
                _prune(buckets, now)
            window_start, count = buckets.get(key, (now, 0))
            if now - window_start >= _WINDOW_S:
                window_start, count = now, 0
            count += 1
            buckets[key] = (window_start, count)
            over_limit = count > limit
            retry_after = int(_WINDOW_S - (now - window_start)) + 1

        if over_limit:
            return JSONResponse(
                {"error": "rate limit exceeded", "limit_per_minute": limit},
                status_code=429,
                headers={"Retry-After": str(max(1, retry_after))},
            )
        return await call_next(request)


def _prune(buckets: dict[str, tuple[float, int]], now: float) -> None:
    """Drop buckets whose window has fully expired; a cheap bound on memory."""
    stale = [ip for ip, (start, _) in buckets.items() if now - start >= _WINDOW_S]
    for ip in stale:
        del buckets[ip]
    # If everything is still fresh, drop the oldest to keep the map bounded.
    if not stale and buckets:
        oldest = min(buckets, key=lambda ip: buckets[ip][0])
        del buckets[oldest]


__all__ = ["bucket_key", "install_rate_limit"]
