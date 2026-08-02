"""Docker availability probe for the ``slow`` Postgres tier (spec 08).

Kept out of ``conftest.py`` so the fast tier can unit-test it without importing a
conftest module.
"""

from __future__ import annotations


def docker_skip_reason() -> str | None:
    """``None`` when a usable Docker daemon is reachable, else a skip reason.

    The Postgres tier is genuinely optional: contributors without Docker still get
    the full fast tier, and CI (ubuntu-latest) always has a daemon. What we must
    never do is let an absent daemon read as a *pass*, hence a skip carrying the
    reason rather than a silent no-op.
    """
    try:
        from testcontainers.core.docker_client import DockerClient

        DockerClient().client.ping()
    except Exception as exc:
        return f"Docker daemon unavailable ({type(exc).__name__}: {exc})"
    return None


__all__ = ["docker_skip_reason"]
