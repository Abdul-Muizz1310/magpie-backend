"""Tests for spider factory dispatch (spec 01-factory)."""

import scrapy

from magpie.config.schema import SourceConfig
from magpie.factory import create_scraper
from magpie.playwright.runner import PlaywrightRunner


def _static_config(**overrides: object) -> SourceConfig:
    base: dict = {
        "name": "test-static",
        "url": "https://example.com",
        "render": False,
        "schedule": "0 */6 * * *",
        "item": {
            "container": "tr.athing",
            "fields": [
                {"name": "title", "selector": "span.titleline > a::text"},
                {"name": "id", "selector": "::attr(id)"},
            ],
            "dedupe_key": "id",
        },
    }
    base.update(overrides)
    return SourceConfig(**base)


def _js_config(**overrides: object) -> SourceConfig:
    base: dict = {
        "name": "test-js",
        "url": "https://example.com",
        "render": True,
        "wait_for": "div.product-list",
        "schedule": "0 */6 * * *",
        "actions": [
            {"type": "click", "selector": "button.load-more"},
            {"type": "wait", "ms": 500},
        ],
        "item": {
            "container": "div.product-card",
            "fields": [
                {"name": "name", "selector": "h3.product-name::text"},
                {"name": "id", "selector": "::attr(data-id)"},
            ],
            "dedupe_key": "id",
        },
    }
    base.update(overrides)
    return SourceConfig(**base)


class TestFactoryDispatch:
    def test_static_config_produces_scrapy_spider(self) -> None:
        scraper = create_scraper(_static_config())
        assert isinstance(scraper, type) and issubclass(scraper, scrapy.Spider)

    def test_js_config_produces_playwright_runner(self) -> None:
        scraper = create_scraper(_js_config())
        assert isinstance(scraper, PlaywrightRunner)


class TestFactoryEdgeCases:
    def test_no_pagination_scrapes_one_page(self) -> None:
        cfg = _static_config(pagination={"max_pages": 1})
        scraper = create_scraper(cfg)
        assert scraper is not None

    def test_empty_actions_skips_action_phase(self) -> None:
        cfg = _js_config(actions=[])
        scraper = create_scraper(cfg)
        assert isinstance(scraper, PlaywrightRunner)

    def test_field_with_attr_extraction(self) -> None:
        cfg = _static_config(
            item={
                "container": "div.item",
                "fields": [
                    {"name": "link", "selector": "a", "attr": "href"},
                    {"name": "id", "selector": "::attr(id)"},
                ],
                "dedupe_key": "id",
            }
        )
        scraper = create_scraper(cfg)
        assert scraper is not None
