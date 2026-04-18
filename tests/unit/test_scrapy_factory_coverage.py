"""Tests for uncovered branches in scrapy/factory.py."""

from __future__ import annotations

from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest

from magpie.config.loader import load_config_from_file
from magpie.config.schema import PaginationDef, SourceConfig
from magpie.scrapy.factory import _extract_items_from_html, build_spider_class, run_spider

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


class TestBuildSpiderClassParse:
    """Cover lines 29-33 and 60-71 of scrapy/factory.py (attr extraction + parse method)."""

    def test_extract_items_attr_field(self) -> None:
        """Test _extract_items_from_html with an attr field on the container."""
        config = SourceConfig(
            name="test-attr",
            url="https://example.com",
            schedule="0 */6 * * *",
            item={
                "container": "div.card",
                "fields": [
                    {"name": "link", "selector": "a", "attr": "data-href"},
                    {"name": "id", "selector": "::attr(data-id)"},
                ],
                "dedupe_key": "id",
            },
        )
        html = """
        <html><body>
        <div class="card" data-id="1" data-href="/page1"><a>Link</a></div>
        <div class="card" data-id="2" data-href="/page2"><a>Link</a></div>
        </body></html>
        """
        items = _extract_items_from_html(html, config)
        assert len(items) == 2
        assert items[0]["link"] == "/page1"

    def test_extract_items_plain_selector_no_pseudo(self) -> None:
        """Selector without ::text or ::attr falls to else branch (line 33)."""
        config = SourceConfig(
            name="test-plain",
            url="https://example.com",
            schedule="0 */6 * * *",
            item={
                "container": "div.wrap",
                "fields": [
                    {"name": "inner", "selector": "span.tag"},
                    {"name": "id", "selector": "::attr(data-id)"},
                ],
                "dedupe_key": "id",
            },
        )
        html = """
        <html><body>
        <div class="wrap" data-id="1"><span class="tag">Hello</span></div>
        </body></html>
        """
        items = _extract_items_from_html(html, config)
        assert len(items) == 1
        assert items[0]["inner"] is not None

    def test_spider_parse_method_extracts_and_paginates(self) -> None:
        """Cover the Spider.parse() method (lines 60-71)."""
        config = load_config_from_file(CONFIGS / "hackernews.yaml")
        config = config.model_copy(
            update={"pagination": PaginationDef(next="a.morelink::attr(href)", max_pages=3)}
        )
        SpiderClass = build_spider_class(config)
        spider = SpiderClass()

        html = (FIXTURES / "hackernews-v1.html").read_text()

        # Build a mock Response
        mock_response = MagicMock()
        mock_response.text = html
        mock_response.css.return_value.get.return_value = "/news?p=2"
        mock_response.follow.return_value = "followed_request"

        results = list(spider.parse(mock_response))
        assert spider._pages_scraped == 1
        assert len(spider._items) >= 5
        # Should yield a follow request
        assert len(results) == 1

    def test_spider_parse_stops_at_max_pages(self) -> None:
        """Parse doesn't follow pagination when max_pages reached."""
        config = load_config_from_file(CONFIGS / "hackernews.yaml")
        config = config.model_copy(
            update={"pagination": PaginationDef(next="a.morelink::attr(href)", max_pages=1)}
        )
        SpiderClass = build_spider_class(config)
        spider = SpiderClass()

        html = (FIXTURES / "hackernews-v1.html").read_text()
        mock_response = MagicMock()
        mock_response.text = html

        results = list(spider.parse(mock_response))
        assert spider._pages_scraped == 1
        # max_pages=1, so no follow
        assert len(results) == 0


class TestXPathExtraction:
    """XPath container + XPath fields should extract equivalently to CSS."""

    def test_extract_items_xpath_container_and_fields(self) -> None:
        config = SourceConfig(
            name="xpath-test",
            url="https://example.com",
            schedule="0 */6 * * *",
            item={
                "container": "//div[@class='card']",
                "container_type": "xpath",
                "fields": [
                    {"name": "title", "selector": ".//h2/text()", "selector_type": "xpath"},
                    {"name": "id", "selector": "./@data-id", "selector_type": "xpath"},
                ],
                "dedupe_key": "id",
            },
        )
        html = """
        <html><body>
        <div class="card" data-id="1"><h2>One</h2></div>
        <div class="card" data-id="2"><h2>Two</h2></div>
        </body></html>
        """
        items = _extract_items_from_html(html, config)
        assert len(items) == 2
        assert items[0]["title"] == "One"
        assert items[0]["id"] == "1"
        assert items[1]["title"] == "Two"
        assert items[1]["id"] == "2"

    def test_extract_items_mixed_css_container_xpath_fields(self) -> None:
        config = SourceConfig(
            name="mixed-test",
            url="https://example.com",
            schedule="0 */6 * * *",
            item={
                "container": "div.card",
                "fields": [
                    {"name": "title", "selector": ".//h2/text()", "selector_type": "xpath"},
                    {"name": "id", "selector": "::attr(data-id)"},
                ],
                "dedupe_key": "id",
            },
        )
        html = """
        <html><body>
        <div class="card" data-id="A"><h2>Alpha</h2></div>
        </body></html>
        """
        items = _extract_items_from_html(html, config)
        assert len(items) == 1
        assert items[0]["title"] == "Alpha"
        assert items[0]["id"] == "A"


class TestRunSpiderPaginationBranches:
    """Cover lines 110-114 of scrapy/factory.py (relative URL resolution)."""

    @pytest.fixture(scope="class")
    def fixture_server(self):
        """Serve fixture HTML files on a local HTTP server."""

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=str(FIXTURES), **kwargs)

            def translate_path(self, path: str) -> str:
                parsed = urlparse(path)
                clean_path = parsed.path
                if clean_path == "/news":
                    return str(FIXTURES / "hackernews-page2.html")
                return super().translate_path(path)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{port}"
        server.shutdown()

    def test_pagination_relative_url_resolution(self, fixture_server: str) -> None:
        """Relative pagination URL (not starting with / or http) is resolved via urljoin."""
        # hackernews-v1.html has morelink href="/news?p=2" which is absolute-path
        # Let's test with a config that follows it
        config = load_config_from_file(CONFIGS / "hackernews.yaml")
        config = config.model_copy(
            update={
                "url": f"{fixture_server}/hackernews-v1.html",
                "pagination": PaginationDef(next="a.morelink::attr(href)", max_pages=2),
            }
        )
        items = run_spider(config)
        assert len(items) >= 10

    def test_pagination_no_next_link_found_breaks(self, fixture_server: str) -> None:
        """When pagination selector returns no match, loop breaks."""
        config = load_config_from_file(CONFIGS / "hackernews.yaml")
        config = config.model_copy(
            update={
                "url": f"{fixture_server}/hackernews-v1.html",
                "pagination": PaginationDef(next="a.nonexistent-link::attr(href)", max_pages=5),
            }
        )
        items = run_spider(config)
        # Only page 1 scraped since next link not found
        assert len(items) == 7

    def test_xpath_pagination_follows_next(self, fixture_server: str) -> None:
        """XPath pagination selector resolves just like the CSS path."""
        config = load_config_from_file(CONFIGS / "hackernews.yaml")
        config = config.model_copy(
            update={
                "url": f"{fixture_server}/hackernews-v1.html",
                "pagination": PaginationDef(
                    next="//a[@class='morelink']/@href",
                    next_type="xpath",
                    max_pages=2,
                ),
            }
        )
        items = run_spider(config)
        assert len(items) >= 10

    def test_pagination_bare_relative_url(self, fixture_server: str) -> None:
        """Relative URL not starting with / or http is resolved via urljoin (lines 110-112)."""
        # We need HTML with a relative link like "page2.html"
        # Create a temp HTML file with a bare relative link

        page2_html = """<html><body>
        <table class="itemlist">
        <tr class="athing" id="99"><td><span class="titleline"><a href="http://example.com/extra">Extra</a></span></td></tr>
        </table>
        </body></html>"""

        page1_html = """<html><body>
        <table class="itemlist">
        <tr class="athing" id="42"><td><span class="titleline"><a href="http://example.com/test">Test</a></span></td></tr>
        </table>
        <a class="barelink" href="hackernews-page2.html">More</a>
        </body></html>"""

        # Write page1 to a temp file in fixtures dir
        page1_path = FIXTURES / "_test_bare_relative.html"
        page1_path.write_text(page1_html, encoding="utf-8")

        try:
            config = load_config_from_file(CONFIGS / "hackernews.yaml")
            config = config.model_copy(
                update={
                    "url": f"{fixture_server}/_test_bare_relative.html",
                    "pagination": PaginationDef(next="a.barelink::attr(href)", max_pages=2),
                }
            )
            items = run_spider(config)
            # Page 1 has 1 item, page 2 (hackernews-page2.html) has items too
            assert len(items) >= 2
        finally:
            page1_path.unlink(missing_ok=True)
