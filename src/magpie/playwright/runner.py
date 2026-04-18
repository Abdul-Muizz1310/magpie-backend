"""Playwright-based scraper for JS-rendered pages."""

from __future__ import annotations

import os
from typing import Any

from magpie.config.schema import SourceConfig
from magpie.scrapy.factory import USER_AGENT, _extract_items_from_html


class PlaywrightRunner:
    """Runs a Playwright browser to scrape JS-rendered pages.

    A real ``User-Agent`` is set on the browser context — Product Hunt,
    Cloudflare-fronted sites, and most anti-bot heuristics reject requests
    from Playwright's default headless signature. Extraction is delegated to
    the shared ``_extract_items_from_html`` so CSS and XPath selectors behave
    identically to the static path.
    """

    def __init__(self, config: SourceConfig) -> None:
        self._config = config

    async def run(self) -> list[dict[str, Any]]:
        """Navigate to the page, execute actions, and extract items."""
        from playwright.async_api import async_playwright

        headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() == "true"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()

            try:
                await page.goto(str(self._config.url), wait_until="domcontentloaded")

                if self._config.wait_for:
                    await page.wait_for_selector(self._config.wait_for, timeout=10000)

                for action in self._config.actions:
                    if action.type == "click" and action.selector:
                        await page.click(action.selector)
                    elif action.type == "wait" and action.ms:
                        await page.wait_for_timeout(action.ms)
                    elif action.type == "scroll":
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    elif action.type == "type" and action.selector and action.text:
                        await page.fill(action.selector, action.text)

                html = await page.content()
                return _extract_items_from_html(html, self._config)
            finally:
                await context.close()
                await browser.close()
