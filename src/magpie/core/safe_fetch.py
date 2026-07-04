"""Redirect-safe HTTP GET that re-validates every hop against the SSRF allowlist.

``host_is_public`` gates the *submitted* host at ``POST /api/sources`` time, but
the fetch paths (scraper + healer) previously used ``follow_redirects=True`` and
never re-checked the redirect target. A source whose public URL 302s to an
internal address (``169.254.169.254``, ``127.0.0.1``, a Docker service name)
would be transparently followed and its body returned to the caller.

These helpers follow redirects manually with ``follow_redirects=False`` and
re-validate each ``Location`` host before following it. The *initial* URL is not
re-validated here — file-origin fixtures legitimately point at loopback and
api-origin URLs are already screened at submission time; only redirect hops (the
attacker-controlled part) are checked.
"""

from __future__ import annotations

import httpx

from magpie.config.schema import host_is_public

MAX_REDIRECTS = 5


class UnsafeRedirectError(Exception):
    """Raised when a redirect points at a non-public (internal) host."""


def _next_hop(resp: httpx.Response) -> str | None:
    """Return the validated absolute URL of a redirect, or None if not a redirect."""
    if not resp.is_redirect:
        return None
    location = resp.headers.get("location")
    if not location:
        return None
    target = resp.url.join(location)
    if not host_is_public(target.host):
        raise UnsafeRedirectError(
            f"Refusing to follow redirect to non-public host {target.host!r} ({target})"
        )
    return str(target)


def safe_get_sync(client: httpx.Client, url: str) -> httpx.Response:
    """Sync GET that re-validates each redirect hop. ``client`` must not auto-follow."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        resp = client.get(current)
        nxt = _next_hop(resp)
        if nxt is None:
            return resp
        current = nxt
    raise UnsafeRedirectError(f"Exceeded {MAX_REDIRECTS} redirects starting from {url}")


async def safe_get_async(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """Async GET that re-validates each redirect hop. ``client`` must not auto-follow."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        resp = await client.get(current)
        nxt = _next_hop(resp)
        if nxt is None:
            return resp
        current = nxt
    raise UnsafeRedirectError(f"Exceeded {MAX_REDIRECTS} redirects starting from {url}")


__all__ = ["MAX_REDIRECTS", "UnsafeRedirectError", "safe_get_async", "safe_get_sync"]
