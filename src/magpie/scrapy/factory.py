"""Build a Scrapy Spider class from a SourceConfig and run it."""

from __future__ import annotations

from typing import Any

import httpx
import scrapy
from parsel import Selector
from scrapy.http import Response

from magpie.config.schema import SourceConfig


def _extract_items_from_html(html: str, config: SourceConfig) -> list[dict[str, Any]]:
    """Extract items from HTML using the config's selectors via parsel.

    Shared extraction logic used by both Scrapy spider and direct HTTP runs.
    Supports both CSS (default) and XPath via ``*_type`` fields on the config.
    """
    sel = Selector(text=html)
    items: list[dict[str, Any]] = []

    container_matches = (
        sel.css(config.item.container)
        if config.item.container_type == "css"
        else sel.xpath(config.item.container)
    )

    for element in container_matches:
        item: dict[str, Any] = {}
        for field in config.item.fields:
            if field.selector_type == "xpath":
                values = element.xpath(field.selector).getall()
            else:
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


def build_spider_class(config: SourceConfig) -> type[scrapy.Spider]:
    """Dynamically create a Scrapy Spider subclass from config."""

    class ConfigSpider(scrapy.Spider):
        name = config.name
        start_urls = [str(config.url)]
        custom_settings = {
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
            "ROBOTSTXT_OBEY": True,
            "LOG_ENABLED": False,
            "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
            "DOWNLOAD_DELAY": 1.0 / config.rate_limit.rps,
        }

        _config = config

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._items: list[dict[str, Any]] = []
            self._pages_scraped: int = 0

        def parse(self, response: Response) -> Any:
            self._pages_scraped += 1
            new_items = _extract_items_from_html(response.text, self._config)
            self._items.extend(new_items)

            # Follow pagination
            if (
                self._config.pagination.next
                and self._pages_scraped < self._config.pagination.max_pages
            ):
                next_sel = self._config.pagination.next
                next_url = (
                    response.xpath(next_sel).get()
                    if self._config.pagination.next_type == "xpath"
                    else response.css(next_sel).get()
                )
                if next_url:
                    yield response.follow(next_url, callback=self.parse)

    return ConfigSpider


def run_spider(config: SourceConfig) -> list[dict[str, Any]]:
    """Run a scraper and return collected items.

    Uses direct HTTP + parsel for extraction (avoids Twisted reactor issues).
    Supports pagination by following next-page links.
    """
    items: list[dict[str, Any]] = []
    url = str(config.url)
    pages_scraped = 0

    with httpx.Client(timeout=30.0) as client:
        while url and pages_scraped < config.pagination.max_pages:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
            pages_scraped += 1

            new_items = _extract_items_from_html(html, config)
            items.extend(new_items)

            # Follow pagination
            if config.pagination.next and pages_scraped < config.pagination.max_pages:
                sel = Selector(text=html)
                next_url = (
                    sel.xpath(config.pagination.next).get()
                    if config.pagination.next_type == "xpath"
                    else sel.css(config.pagination.next).get()
                )
                if next_url:
                    # Resolve relative URLs
                    if next_url.startswith("/") or next_url.startswith("http"):
                        if next_url.startswith("/"):
                            # Build absolute URL from base
                            from urllib.parse import urljoin

                            next_url = urljoin(url, next_url)
                        url = next_url
                    else:
                        from urllib.parse import urljoin

                        url = urljoin(url, next_url)
                else:
                    break
            else:
                break

    return items
