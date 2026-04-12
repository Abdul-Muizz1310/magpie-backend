"""Factory dispatch: config -> Scrapy spider class or Playwright runner."""

from __future__ import annotations

from typing import Any

from magpie.config.schema import SourceConfig
from magpie.playwright.runner import PlaywrightRunner
from magpie.scrapy.factory import build_spider_class


def create_scraper(config: SourceConfig) -> type[Any] | PlaywrightRunner:
    """Create the appropriate scraper for a config.

    Returns a Scrapy Spider class (render=false) or a PlaywrightRunner instance (render=true).
    """
    if config.render:
        return PlaywrightRunner(config)
    return build_spider_class(config)
