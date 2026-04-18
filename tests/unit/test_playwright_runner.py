"""Tests for PlaywrightRunner covering JS-rendered scraping flow."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from magpie.config.schema import SourceConfig
from magpie.playwright.runner import PlaywrightRunner
from magpie.scrapy.factory import _extract_items_from_html


def _js_config(**overrides: object) -> SourceConfig:
    base: dict = {
        "name": "test-js",
        "url": "https://example.com",
        "render": True,
        "wait_for": "div.loaded",
        "schedule": "0 */6 * * *",
        "actions": [
            {"type": "click", "selector": "button.more"},
            {"type": "wait", "ms": 500},
            {"type": "scroll"},
            {"type": "type", "selector": "input.search", "text": "hello"},
        ],
        "item": {
            "container": "div.item",
            "fields": [
                {"name": "title", "selector": "h2::text"},
                {"name": "id", "selector": "::attr(data-id)"},
            ],
            "dedupe_key": "id",
        },
    }
    base.update(overrides)
    return SourceConfig(**base)


def _js_config_no_wait(**overrides: object) -> SourceConfig:
    base: dict = {
        "name": "test-js-nowait",
        "url": "https://example.com",
        "render": True,
        "schedule": "0 */6 * * *",
        "actions": [],
        "item": {
            "container": "div.item",
            "fields": [
                {"name": "title", "selector": "h2::text"},
                {"name": "id", "selector": "::attr(data-id)"},
            ],
            "dedupe_key": "id",
        },
    }
    base.update(overrides)
    return SourceConfig(**base)


SAMPLE_HTML = """
<html><body>
<div class="item" data-id="1"><h2>Item One</h2></div>
<div class="item" data-id="2"><h2>Item Two</h2></div>
</body></html>
"""


def _make_playwright_mock(mock_page, mock_browser, mock_context):
    """Build a mock async_playwright context manager.

    Mirrors the real flow: ``p.chromium.launch`` → ``browser.new_context`` →
    ``context.new_page``. Tests can reach any layer via their own mocks.
    """
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.close = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_chromium = MagicMock()
    mock_chromium.launch = AsyncMock(return_value=mock_browser)

    mock_pw = MagicMock()
    mock_pw.chromium = mock_chromium

    mock_pw_ctx = AsyncMock()
    mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
    mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=mock_pw_ctx)


@pytest.fixture(autouse=True)
def _mock_playwright_module():
    """Ensure playwright.async_api is mockable even if not installed."""
    pw_module = ModuleType("playwright")
    pw_api_module = ModuleType("playwright.async_api")
    pw_api_module.async_playwright = MagicMock()  # type: ignore[attr-defined]

    saved = {}
    for name in ("playwright", "playwright.async_api"):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = pw_module if name == "playwright" else pw_api_module

    yield pw_api_module

    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


class TestPlaywrightRunnerRun:
    @pytest.mark.asyncio
    async def test_run_navigates_waits_and_extracts(
        self, _mock_playwright_module: ModuleType
    ) -> None:
        """Full flow: launch browser, new context with UA, goto, wait_for, actions, extract."""
        cfg = _js_config()
        runner = PlaywrightRunner(cfg)

        mock_page = AsyncMock()
        mock_page.content = AsyncMock(return_value=SAMPLE_HTML)
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.click = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        mock_page.evaluate = AsyncMock()
        mock_page.fill = AsyncMock()

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_fn = _make_playwright_mock(mock_page, mock_browser, mock_context)
        _mock_playwright_module.async_playwright = mock_fn  # type: ignore[attr-defined]

        items = await runner.run()

        assert len(items) == 2
        assert items[0]["title"] == "Item One"
        assert items[0]["id"] == "1"

        mock_page.goto.assert_called_once()
        mock_page.wait_for_selector.assert_called_once_with("div.loaded", timeout=10000)
        mock_page.click.assert_called_once_with("button.more")
        mock_page.wait_for_timeout.assert_called_once_with(500)
        mock_page.evaluate.assert_called_once_with("window.scrollTo(0, document.body.scrollHeight)")
        mock_page.fill.assert_called_once_with("input.search", "hello")
        mock_browser.close.assert_called_once()
        mock_context.close.assert_called_once()

        # User-Agent was set on the context.
        new_context_kwargs = mock_browser.new_context.await_args.kwargs
        assert "magpie" in new_context_kwargs["user_agent"]

    @pytest.mark.asyncio
    async def test_run_without_wait_for(self, _mock_playwright_module: ModuleType) -> None:
        """When wait_for is None, skip wait_for_selector."""
        cfg = _js_config_no_wait()
        runner = PlaywrightRunner(cfg)

        mock_page = AsyncMock()
        mock_page.content = AsyncMock(return_value="<html><body></body></html>")
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_fn = _make_playwright_mock(mock_page, mock_browser, mock_context)
        _mock_playwright_module.async_playwright = mock_fn  # type: ignore[attr-defined]

        items = await runner.run()

        assert items == []
        mock_page.wait_for_selector.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_closes_browser_on_error(self, _mock_playwright_module: ModuleType) -> None:
        """Browser is closed even when page.goto raises."""
        cfg = _js_config_no_wait()
        runner = PlaywrightRunner(cfg)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(side_effect=RuntimeError("network error"))

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_fn = _make_playwright_mock(mock_page, mock_browser, mock_context)
        _mock_playwright_module.async_playwright = mock_fn  # type: ignore[attr-defined]

        with pytest.raises(RuntimeError, match="network error"):
            await runner.run()

        mock_browser.close.assert_called_once()
        mock_context.close.assert_called_once()


class TestPlaywrightExtractionParity:
    """The Playwright runner delegates to _extract_items_from_html, so the
    tests that used to hit its private ``_extract_items`` method now drive the
    shared extractor directly. Same behaviour, same coverage, one code path.
    """

    def test_extract_text_selectors(self) -> None:
        cfg = _js_config()
        html = """
        <html><body>
        <div class="item" data-id="10"><h2>Title A</h2></div>
        <div class="item" data-id="20"><h2>Title B</h2></div>
        </body></html>
        """
        items = _extract_items_from_html(html, cfg)
        assert len(items) == 2
        assert items[0]["title"] == "Title A"
        assert items[1]["id"] == "20"

    def test_extract_attr_field(self) -> None:
        cfg = SourceConfig(
            name="test-attr",
            url="https://example.com",
            render=True,
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
        items = _extract_items_from_html(html, cfg)
        assert len(items) == 2
        assert items[0]["link"] == "/page1"

    def test_extract_plain_selector_no_pseudo(self) -> None:
        cfg = SourceConfig(
            name="test-plain",
            url="https://example.com",
            render=True,
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
        items = _extract_items_from_html(html, cfg)
        assert len(items) == 1
        assert items[0]["inner"] is not None

    def test_extract_skips_all_none_items(self) -> None:
        cfg = _js_config()
        html = """
        <html><body>
        <div class="item"><p>No h2 or data-id</p></div>
        </body></html>
        """
        items = _extract_items_from_html(html, cfg)
        assert items == []

    def test_extract_attr_missing_returns_none(self) -> None:
        cfg = SourceConfig(
            name="test-missing-attr",
            url="https://example.com",
            render=True,
            schedule="0 */6 * * *",
            item={
                "container": "div.card",
                "fields": [
                    {"name": "link", "selector": "a", "attr": "data-missing"},
                    {"name": "name", "selector": "a::text"},
                ],
                "dedupe_key": "name",
            },
        )
        html = """
        <html><body>
        <div class="card"><a href="/page">Click</a></div>
        </body></html>
        """
        items = _extract_items_from_html(html, cfg)
        assert len(items) == 1
        assert items[0]["link"] is None
        assert items[0]["name"] == "Click"
