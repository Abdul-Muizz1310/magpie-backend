"""E2E test for full heal flow: broken config -> healer -> PR (spec 03-healer)."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from magpie.config.loader import load_config_from_file
from magpie.healer.detector import should_heal
from magpie.healer.selector_fixer import fix_selector
from magpie.healer.validator import validate_selector

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


class TestHealFlowE2E:
    @pytest.mark.asyncio
    async def test_broken_config_triggers_heal_flow(self) -> None:
        """Full flow: broken config -> 0 items -> healer -> PR body matches."""
        # 1. Load the deliberately broken config
        config = load_config_from_file(CONFIGS / "demo-broken.yaml")

        # 2. Simulate a scrape that returns 0 items
        item_count = 0
        assert should_heal(item_count=item_count, min_items=config.health.min_items)

        # 3. Load the "broken" HTML (original selectors don't match)
        html = (FIXTURES / "hackernews-v2-broken.html").read_text()

        # 4. Verify old selector returns nothing
        old_results = validate_selector(html, config.item.fields[0].selector)
        assert old_results == []

        # 5. Mock LLM to propose a fix
        mock_response = {
            "selector": "span.storylink > a",
            "confidence": 0.92,
            "reasoning": "titleline was renamed to storylink",
            "sample_values": ["First Article Title", "Second Article Title"],
        }
        with patch(
            "magpie.healer.selector_fixer._call_llm",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await fix_selector(
                field_name="title",
                old_selector=config.item.fields[0].selector,
                html=html,
                old_samples=["First Article Title"],
            )

        # 6. Validate the proposed selector works on the snapshot
        assert result is not None
        assert result["selector"] is not None
        new_results = validate_selector(html, result["selector"])
        assert len(new_results) >= 1

        # 7. Verify PR body content would be correct
        assert result["confidence"] > 0.5
        assert "storylink" in result["reasoning"]

    @pytest.mark.asyncio
    async def test_heal_flow_with_unfixable_selector(self) -> None:
        """LLM cannot fix -> no PR created."""
        config = load_config_from_file(CONFIGS / "demo-broken.yaml")
        html = (FIXTURES / "hackernews-v2-broken.html").read_text()

        mock_response = {
            "selector": None,
            "confidence": 0.0,
            "reasoning": "Page structure changed completely",
            "sample_values": [],
        }
        with patch(
            "magpie.healer.selector_fixer._call_llm",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await fix_selector(
                field_name="title",
                old_selector=config.item.fields[0].selector,
                html=html,
                old_samples=[],
            )
        assert result is not None
        assert result["selector"] is None
