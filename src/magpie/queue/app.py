"""Procrastinate ``App`` for magpie.

The connector talks to the same Postgres instance as SQLAlchemy, but via
``psycopg`` directly (Procrastinate does not support async SQLAlchemy as a
connector). Procrastinate expects a plain ``postgresql://`` URL — any
``+asyncpg`` driver suffix we added for SQLAlchemy must be stripped here.
"""

from __future__ import annotations

import os

from procrastinate import App, PsycopgConnector


def _procrastinate_conninfo() -> str:
    """Return a psycopg-compatible conninfo string from ``DATABASE_URL``.

    Empty string is returned when nothing is configured — good for unit tests
    that swap the connector for ``InMemoryConnector`` before the app ever
    opens a real connection.
    """
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    # Strip any SQLAlchemy-style driver suffix so psycopg accepts it.
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            url = "postgresql://" + url[len(prefix) :]
            break
    return url


queue_app = App(
    connector=PsycopgConnector(conninfo=_procrastinate_conninfo()),
    import_paths=["magpie.queue.tasks"],
)


__all__ = ["queue_app"]
