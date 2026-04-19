"""Playwright-based scraper for JS-rendered pages."""

from __future__ import annotations

import logging
import os
from typing import Any

from magpie.config.schema import SourceConfig
from magpie.scrapy.factory import USER_AGENT, _extract_items_from_html

log = logging.getLogger("magpie.playwright")


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

    async def fetch_html(self) -> str:
        """Navigate + execute actions, return the rendered DOM as a string.

        Shared by :meth:`run` (which extracts items from the returned HTML)
        and the healer's fetch path, which needs the raw DOM to ask the LLM
        for a replacement selector.
        """
        from playwright.async_api import async_playwright

        headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() == "true"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()

            try:
                await page.goto(str(self._config.url), wait_until="domcontentloaded")

                if self._config.wait_for:
                    try:
                        await page.wait_for_selector(self._config.wait_for, timeout=10000)
                    except Exception as exc:
                        # Don't abort the run — the healer needs the raw HTML to
                        # propose a replacement selector. Log and press on.
                        log.warning(
                            "wait_for_selector %r timed out on %s: %s",
                            self._config.wait_for,
                            self._config.name,
                            exc,
                        )

                for action in self._config.actions:
                    if action.type == "click" and action.selector:
                        await page.click(action.selector)
                    elif action.type == "wait" and action.ms:
                        await page.wait_for_timeout(action.ms)
                    elif action.type == "scroll":
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    elif action.type == "type" and action.selector and action.text:
                        await page.fill(action.selector, action.text)

                return await page.content()
            finally:
                await context.close()
                await browser.close()

    async def run(self) -> list[dict[str, Any]]:
        """Navigate to the page, execute actions, and extract items."""
        html = await self.fetch_html()
        return _extract_items_from_html(html, self._config)
