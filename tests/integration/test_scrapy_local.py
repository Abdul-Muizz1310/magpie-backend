"""Integration tests for Scrapy spider against local fixture server (spec 01-factory)."""

import asyncio
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread

import pytest

from magpie.config.loader import load_config_from_file
from magpie.factory import create_scraper
from magpie.scrapy.factory import run_spider

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


@pytest.fixture(scope="module")
def fixture_server():
    """Serve fixture HTML files on a local HTTP server."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FIXTURES), **kwargs)

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
        # Override URL to point at local fixture server
        config = config.model_copy(
            update={"url": f"{fixture_server}/hackernews-v1.html"}
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
                "pagination": {"next": "a.morelink::attr(href)", "max_pages": 2},
            }
        )
        items = run_spider(config)
        # Should include items from both pages
        assert len(items) >= 7

    def test_spider_stops_at_max_pages(self, fixture_server: str) -> None:
        """Spider respects max_pages limit."""
        config = load_config_from_file(CONFIGS / "hackernews.yaml")
        config = config.model_copy(
            update={
                "url": f"{fixture_server}/hackernews-v1.html",
                "pagination": {"next": "a.morelink::attr(href)", "max_pages": 1},
            }
        )
        items = run_spider(config)
        # Only page 1 items
        assert len(items) <= 7

    def test_zero_items_from_broken_selector(self, fixture_server: str) -> None:
        """Broken selector returns empty list (not crash)."""
        config = load_config_from_file(CONFIGS / "demo-broken.yaml")
        config = config.model_copy(
            update={"url": f"{fixture_server}/hackernews-v1.html"}
        )
        items = run_spider(config)
        assert items == []
