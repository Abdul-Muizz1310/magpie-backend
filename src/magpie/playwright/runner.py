"""Playwright-based scraper for JS-rendered pages."""

from __future__ import annotations

import os
from typing import Any

from parsel import Selector

from magpie.config.schema import SourceConfig


class PlaywrightRunner:
    """Runs a Playwright browser to scrape JS-rendered pages."""

    def __init__(self, config: SourceConfig) -> None:
        self._config = config

    async def run(self) -> list[dict[str, Any]]:
        """Navigate to the page, execute actions, and extract items."""
        from playwright.async_api import async_playwright

        headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() == "true"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            page = await browser.new_page()

            try:
                await page.goto(str(self._config.url), wait_until="domcontentloaded")

                # Wait for the target element if specified
                if self._config.wait_for:
                    await page.wait_for_selector(self._config.wait_for, timeout=10000)

                # Execute actions in order
                for action in self._config.actions:
                    if action.type == "click" and action.selector:
                        await page.click(action.selector)
                    elif action.type == "wait" and action.ms:
                        await page.wait_for_timeout(action.ms)
                    elif action.type == "scroll":
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    elif action.type == "type" and action.selector and action.text:
                        await page.fill(action.selector, action.text)

                # Get page HTML and parse with parsel
                html = await page.content()
                return self._extract_items(html)
            finally:
                await browser.close()

    def _extract_items(self, html: str) -> list[dict[str, Any]]:
        """Extract items from HTML using CSS selectors via parsel."""
        sel = Selector(text=html)
        items: list[dict[str, Any]] = []

        for element in sel.css(self._config.item.container):
            item: dict[str, Any] = {}
            for field in self._config.item.fields:
                selector = field.selector
                if "::text" in selector or "::attr" in selector:
                    values = element.css(selector).getall()
                elif field.attr:
                    val = element.attrib.get(field.attr)
                    values = [val] if val else []
                else:
                    values = element.css(selector).getall()
                item[field.name] = values[0] if values else None

            if any(v is not None for v in item.values()):
                items.append(item)

        return items
