"""Integration tests for Scrapy spider against local fixture server (spec 01-factory)."""

from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import pytest

from magpie.config.loader import load_config_from_file
from magpie.config.schema import PaginationDef, SourceConfig
from magpie.scrapy.factory import run_spider

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"

# Map virtual paths to fixture files for pagination
_ROUTE_MAP = {
    "/news": "hackernews-page2.html",
}


@pytest.fixture(scope="module")
def fixture_server():
    """Serve fixture HTML files on a local HTTP server with route mapping."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FIXTURES), **kwargs)

        def translate_path(self, path: str) -> str:
            """Override to handle virtual routes like /news?p=2."""
            parsed = urlparse(path)
            clean_path = parsed.path
            if clean_path in _ROUTE_MAP:
                return str(FIXTURES / _ROUTE_MAP[clean_path])
            return super().translate_path(path)

        def log_message(self, format, *args):
            pass  # suppress logs

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


class TestScrapyLocalIntegration:
    def test_hackernews_spider_extracts_items(self, fixture_server: str) -> None:
        """Scrapy spider against hackernews fixture, expect >= 5 items."""
        config = load_config_from_file(CONFIGS / "hackernews.yaml")
        config = config.model_copy(
            update={
                "url": f"{fixture_server}/hackernews-v1.html",
                "pagination": PaginationDef(max_pages=1),
            }
        )
        items = run_spider(config)
        assert len(items) >= 5
        assert all("title" in item for item in items)
        assert all("id" in item for item in items)

    def test_spider_follows_pagination(self, fixture_server: str) -> None:
        """Spider follows pagination to page 2."""
        config = load_config_from_file(CONFIGS / "hackernews.yaml")
        config = config.model_copy(
            update={
                "url": f"{fixture_server}/hackernews-v1.html",
                "pagination": PaginationDef(next="a.morelink::attr(href)", max_pages=2),
            }
        )
        items = run_spider(config)
        # Page 1 has 7 items, page 2 has 3 items = 10 total
        assert len(items) >= 10

    def test_spider_stops_at_max_pages(self, fixture_server: str) -> None:
        """Spider respects max_pages limit."""
        config = load_config_from_file(CONFIGS / "hackernews.yaml")
        config = config.model_copy(
            update={
                "url": f"{fixture_server}/hackernews-v1.html",
                "pagination": PaginationDef(next="a.morelink::attr(href)", max_pages=1),
            }
        )
        items = run_spider(config)
        # Only page 1 items
        assert len(items) == 7

    def test_zero_items_from_broken_selector(self, fixture_server: str) -> None:
        """Broken title selector yields None for title; the spider itself doesn't crash."""
        config = SourceConfig(
            name="inline-broken",
            url=f"{fixture_server}/hackernews-v1.html",  # type: ignore[arg-type]
            schedule="0 0 * * 0",
            item={  # type: ignore[arg-type]
                "container": "tr.athing",
                "fields": [
                    {"name": "title", "selector": "span.nonexistent-class > a::text"},
                    {"name": "id", "selector": "::attr(id)"},
                ],
                "dedupe_key": "id",
            },
        )
        items = run_spider(config)
        for item in items:
            assert item.get("title") is None
