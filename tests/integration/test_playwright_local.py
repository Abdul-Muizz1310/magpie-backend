"""Integration tests for Playwright runner against local fixture server (spec 01-factory)."""

from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread

import pytest

from magpie.config.schema import SourceConfig
from magpie.playwright.runner import PlaywrightRunner

try:
    from playwright.async_api import async_playwright  # noqa: F401

    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

pytestmark = pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="Playwright browsers not installed")

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"


@pytest.fixture(scope="module")
def fixture_server():
    """Serve fixture HTML files on a local HTTP server."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FIXTURES), **kwargs)

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _shop_config(base_url: str) -> SourceConfig:
    return SourceConfig(
        name="test-shop",
        url=f"{base_url}/fake-shop.html",
        render=True,
        wait_for="div.product-list",
        schedule="0 */12 * * *",
        actions=[],
        item={
            "container": "div.product-card",
            "fields": [
                {"name": "name", "selector": "h3.product-name::text"},
                {"name": "price", "selector": "span.product-price::text"},
                {"name": "id", "selector": "::attr(data-id)"},
            ],
            "dedupe_key": "id",
        },
    )


class TestPlaywrightLocalIntegration:
    @pytest.mark.asyncio
    async def test_extracts_items_from_js_page(self, fixture_server: str) -> None:
        cfg = _shop_config(fixture_server)
        runner = PlaywrightRunner(cfg)
        items = await runner.run()
        assert len(items) == 3
        assert all("name" in item for item in items)

    @pytest.mark.asyncio
    async def test_handles_missing_wait_for_timeout(self, fixture_server: str, caplog) -> None:
        """wait_for_selector timeouts are now logged and the run continues.

        The previous behaviour (propagate the exception) meant the healer
        could never see the post-timeout HTML — it would only ever know
        "timed out". Soft-failing lets the page's extraction run and gives
        ``heal_source`` raw HTML to reason about.
        """
        cfg = SourceConfig(
            name="test-timeout",
            url=f"{fixture_server}/fake-shop.html",
            render=True,
            wait_for="div.nonexistent-element",
            schedule="0 */12 * * *",
            item={
                "container": "div.product-card",
                "fields": [
                    {"name": "name", "selector": "h3.product-name::text"},
                    {"name": "id", "selector": "::attr(data-id)"},
                ],
                "dedupe_key": "id",
            },
        )
        runner = PlaywrightRunner(cfg)
        items = await runner.run()
        # Extraction still happens — returns the shop's items since the page
        # rendered fine; only the guard selector was missing.
        assert items  # non-empty
        assert any("wait_for_selector" in r.message for r in caplog.records)
