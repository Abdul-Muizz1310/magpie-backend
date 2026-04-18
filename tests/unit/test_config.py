"""Tests for config loader + Pydantic schema (spec 00-config)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from magpie.config.loader import load_config
from magpie.config.registry import load_all_configs
from magpie.config.schema import (
    SourceConfig,
)

FIXTURES = Path(__file__).resolve().parent.parent.parent / "configs"


# ── helpers ──────────────────────────────────────────────────────────────────


def _minimal_config(**overrides: object) -> dict:
    """Return a minimal valid config dict, with optional overrides."""
    base: dict = {
        "name": "test-source",
        "url": "https://example.com",
        "schedule": "0 */6 * * *",
        "item": {
            "container": "div.item",
            "fields": [{"name": "title", "selector": "h2::text"}],
            "dedupe_key": "title",
        },
    }
    base.update(overrides)
    return base


# ── Happy path ───────────────────────────────────────────────────────────────


class TestConfigHappyPath:
    def test_valid_static_config_parses(self) -> None:
        cfg = SourceConfig(**_minimal_config())
        assert cfg.name == "test-source"
        assert cfg.render is False
        assert str(cfg.url) == "https://example.com/"

    def test_valid_js_render_config_parses(self) -> None:
        cfg = SourceConfig(
            **_minimal_config(
                render=True,
                wait_for="div.loaded",
                actions=[{"type": "click", "selector": "button.more"}],
            )
        )
        assert cfg.render is True
        assert cfg.wait_for == "div.loaded"
        assert len(cfg.actions) == 1

    def test_optional_fields_use_defaults(self) -> None:
        cfg = SourceConfig(**_minimal_config())
        assert cfg.pagination.max_pages == 1
        assert cfg.health.min_items == 1
        assert cfg.render is False
        assert cfg.description == ""
        assert cfg.actions == []

    def test_registry_loads_all_shipped_configs(self) -> None:
        configs = load_all_configs(FIXTURES)
        names = {c.name for c in configs}
        assert names == {
            "hackernews",
            "arxiv-cs",
            "weather-live",
            "demo-broken",
            "lobsters",
            "huggingface-papers",
            "github-trending",
            "producthunt-today",
            "wikipedia-current-events",
        }

    def test_no_pagination_is_valid(self) -> None:
        cfg = SourceConfig(**_minimal_config())
        assert cfg.pagination.next is None
        assert cfg.pagination.max_pages == 1


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestConfigEdgeCases:
    def test_unicode_description(self) -> None:
        cfg = SourceConfig(**_minimal_config(description="Scraper für Nachrichten 日本語"))
        assert "日本語" in cfg.description

    def test_custom_rate_limit(self) -> None:
        cfg = SourceConfig(**_minimal_config(rate_limit={"rps": 10}))
        assert cfg.rate_limit.rps == 10

    def test_minimal_valid_config(self) -> None:
        cfg = SourceConfig(**_minimal_config())
        assert cfg.name == "test-source"


# ── Failure cases ────────────────────────────────────────────────────────────


class TestConfigFailures:
    def test_uppercase_name_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(name="TestSource"))

    def test_name_with_spaces_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(name="test source"))

    def test_name_with_underscores_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(name="test_source"))

    def test_empty_name_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(name=""))

    def test_invalid_url_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(url="not-a-url"))

    def test_empty_fields_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(item={"container": "div", "fields": [], "dedupe_key": "x"})
            )

    def test_dedupe_key_nonexistent_field_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    item={
                        "container": "div",
                        "fields": [{"name": "title", "selector": "h2::text"}],
                        "dedupe_key": "nonexistent",
                    }
                )
            )

    def test_static_config_with_actions_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    render=False,
                    actions=[{"type": "click", "selector": "button"}],
                )
            )

    def test_static_config_with_wait_for_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(render=False, wait_for="div.loaded"))

    def test_click_action_without_selector_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    render=True,
                    actions=[{"type": "click"}],
                )
            )

    def test_wait_action_without_ms_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    render=True,
                    actions=[{"type": "wait"}],
                )
            )

    def test_type_action_without_selector_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    render=True,
                    actions=[{"type": "type", "text": "hello"}],
                )
            )

    def test_max_pages_zero_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(pagination={"max_pages": 0}))

    def test_negative_min_items_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(health={"min_items": -1}))

    def test_malformed_yaml_raises(self) -> None:
        import yaml

        with pytest.raises((ValidationError, ValueError, yaml.YAMLError)):
            load_config("not: valid: yaml: {{{}}")

    def test_unknown_top_level_keys_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(unknown_field="surprise"))

    def test_duplicate_field_names_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    item={
                        "container": "div",
                        "fields": [
                            {"name": "title", "selector": "h2::text"},
                            {"name": "title", "selector": "h3::text"},
                        ],
                        "dedupe_key": "title",
                    }
                )
            )


# ── XPath and selector validation ───────────────────────────────────────────


class TestSelectorTypes:
    def test_xpath_field_accepted(self) -> None:
        cfg = SourceConfig(
            **_minimal_config(
                item={
                    "container": "//div[@class='item']",
                    "container_type": "xpath",
                    "fields": [
                        {"name": "title", "selector": ".//h2/text()", "selector_type": "xpath"},
                    ],
                    "dedupe_key": "title",
                }
            )
        )
        assert cfg.item.container_type == "xpath"
        assert cfg.item.fields[0].selector_type == "xpath"

    def test_default_selector_type_is_css(self) -> None:
        cfg = SourceConfig(**_minimal_config())
        assert cfg.item.container_type == "css"
        assert cfg.item.fields[0].selector_type == "css"
        assert cfg.pagination.next_type == "css"

    def test_xpath_pagination_accepted(self) -> None:
        cfg = SourceConfig(
            **_minimal_config(
                pagination={"next": "//a[@class='next']/@href", "next_type": "xpath"},
            )
        )
        assert cfg.pagination.next_type == "xpath"

    def test_mixed_css_container_xpath_fields(self) -> None:
        cfg = SourceConfig(
            **_minimal_config(
                item={
                    "container": "div.item",
                    "fields": [
                        {"name": "title", "selector": ".//h2/text()", "selector_type": "xpath"},
                    ],
                    "dedupe_key": "title",
                }
            )
        )
        assert cfg.item.container_type == "css"
        assert cfg.item.fields[0].selector_type == "xpath"


class TestSelectorValidation:
    def test_invalid_css_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    item={
                        "container": "div[[[garbage",
                        "fields": [{"name": "title", "selector": "h2::text"}],
                        "dedupe_key": "title",
                    }
                )
            )

    def test_invalid_xpath_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    item={
                        "container": "//div[@",
                        "container_type": "xpath",
                        "fields": [{"name": "title", "selector": "h2::text"}],
                        "dedupe_key": "title",
                    }
                )
            )

    def test_invalid_css_field_selector_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    item={
                        "container": "div.item",
                        "fields": [{"name": "title", "selector": "h2[[[bad"}],
                        "dedupe_key": "title",
                    }
                )
            )

    def test_invalid_xpath_field_selector_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    item={
                        "container": "div.item",
                        "fields": [
                            {"name": "title", "selector": "//h2[@", "selector_type": "xpath"},
                        ],
                        "dedupe_key": "title",
                    }
                )
            )

    def test_invalid_pagination_selector_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(**_minimal_config(pagination={"next": "a[[broken"}))

    def test_unknown_selector_type_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            SourceConfig(
                **_minimal_config(
                    item={
                        "container": "div.item",
                        "fields": [
                            {"name": "title", "selector": "h2", "selector_type": "sizzle"},
                        ],
                        "dedupe_key": "title",
                    }
                )
            )
