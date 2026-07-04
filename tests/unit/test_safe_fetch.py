"""Tests for redirect-safe fetch (SSRF re-validation across redirect hops)."""

from __future__ import annotations

import httpx
import pytest

from magpie.core.safe_fetch import (
    MAX_REDIRECTS,
    UnsafeRedirectError,
    safe_get_async,
    safe_get_sync,
)


def _client(handler: object, *, is_async: bool = False) -> httpx.Client | httpx.AsyncClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    if is_async:
        return httpx.AsyncClient(transport=transport, follow_redirects=False)
    return httpx.Client(transport=transport, follow_redirects=False)


class TestSafeGetSync:
    def test_non_redirect_returns_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        with _client(handler) as client:  # type: ignore[union-attr]
            resp = safe_get_sync(client, "https://example.com/x")
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_follows_public_redirect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "https://example.org/final"})
            return httpx.Response(200, text="landed")

        with _client(handler) as client:  # type: ignore[union-attr]
            resp = safe_get_sync(client, "https://example.com/start")
        assert resp.text == "landed"

    def test_rejects_redirect_to_metadata_endpoint(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )

        with _client(handler) as client, pytest.raises(UnsafeRedirectError):  # type: ignore[union-attr]
            safe_get_sync(client, "https://example.com/start")

    def test_rejects_redirect_to_loopback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/internal"})

        with _client(handler) as client, pytest.raises(UnsafeRedirectError):  # type: ignore[union-attr]
            safe_get_sync(client, "https://example.com/start")

    def test_rejects_too_many_redirects(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # Always redirect to another public host -> loop exhausts the budget.
            return httpx.Response(302, headers={"location": "https://example.org/next"})

        with _client(handler) as client, pytest.raises(UnsafeRedirectError):  # type: ignore[union-attr]
            safe_get_sync(client, "https://example.org/next")


class TestSafeGetAsync:
    @pytest.mark.asyncio
    async def test_rejects_internal_redirect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://10.0.0.5/admin"})

        client = _client(handler, is_async=True)
        try:
            with pytest.raises(UnsafeRedirectError):
                await safe_get_async(client, "https://example.com/start")  # type: ignore[arg-type]
        finally:
            await client.aclose()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_follows_public_redirect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(301, headers={"location": "https://example.org/final"})
            return httpx.Response(200, text="landed")

        client = _client(handler, is_async=True)
        try:
            resp = await safe_get_async(client, "https://example.com/start")  # type: ignore[arg-type]
            assert resp.text == "landed"
        finally:
            await client.aclose()  # type: ignore[union-attr]


def test_max_redirects_is_bounded() -> None:
    assert MAX_REDIRECTS >= 1
