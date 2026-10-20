"""Per-request query observability through slowquery-detective 0.2 (spec 11).

bastion tags the calls it makes with ``X-Request-Id: R`` and then asks
``GET /_slowquery/queries?request_id=R`` which queries R caused. Only bastion may
ask: the platform-token middleware has already authenticated the request (the
routes are not public), and :func:`authorize_bastion` admits only a principal
whose token bastion's key signed.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request
from slowquery_detective import install
from sqlalchemy.ext.asyncio import AsyncEngine

from magpie.platform.platform_token import SERVICE_ID
from magpie.platform.principal import Minter, principal_of

_POSTGRES_SCHEMES: Final = (
    "postgres://",
    "postgresql://",
    "postgresql+asyncpg://",
    "postgresql+psycopg://",
)


def store_url_for(database_url: str | None) -> str | None:
    """The service's own Postgres URL for the store; ``None`` (in-memory) for anything else."""
    url = (database_url or "").strip()
    return url if url.startswith(_POSTGRES_SCHEMES) else None


async def authorize_bastion(request: Request) -> bool:
    """``True`` only for a request authenticated with bastion's key."""
    principal = principal_of(request)
    return principal is not None and principal.minter is Minter.bastion


def install_query_observability(
    app: FastAPI, engine: AsyncEngine, database_url: str | None
) -> None:
    """Install slowquery-detective. Call after every ``add_middleware`` of the app.

    The library's request tagger must be the outermost middleware, so every
    statement a request causes is attributed to its ``X-Request-Id``.
    """
    install(
        app,
        engine,
        service=SERVICE_ID,
        store_url=store_url_for(database_url),
        authorize=authorize_bastion,
    )


__all__ = ["authorize_bastion", "install_query_observability", "store_url_for"]
