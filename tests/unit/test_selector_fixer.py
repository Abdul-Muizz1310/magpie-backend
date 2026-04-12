"""Tests for healer selector fixer (spec 03-healer), LLM mocked."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from magpie.healer.detector import should_heal
from magpie.healer.selector_fixer import fix_selector
from magpie.healer.validator import validate_selector

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"


class TestDetector:
    def test_triggers_when_items_below_min(self) -> None:
        assert should_heal(item_count=0, min_items=20) is True

    def test_does_not_trigger_when_items_meet_min(self) -> None:
        assert should_heal(item_count=20, min_items=20) is False

    def test_does_not_trigger_when_items_exceed_min(self) -> None:
        assert should_heal(item_count=30, min_items=20) is False

    def test_does_not_trigger_when_min_items_zero(self) -> None:
        assert should_heal(item_count=0, min_items=0) is False


class TestValidator:
    def test_valid_selector_returns_items(self) -> None:
        html = (FIXTURES / "hackernews-v1.html").read_text()
        results = validate_selector(html, "span.titleline > a")
        assert len(results) >= 5

    def test_broken_selector_returns_empty(self) -> None:
        html = (FIXTURES / "hackernews-v1.html").read_text()
        results = validate_selector(html, "span.nonexistent")
        assert results == []

    def test_selector_on_broken_html(self) -> None:
        html = (FIXTURES / "hackernews-v2-broken.html").read_text()
        results = validate_selector(html, "span.titleline > a")
        assert results == []


class TestSelectorFixer:
    @pytest.mark.asyncio
    async def test_llm_returns_valid_selector(self) -> None:
        mock_response = {
            "selector": "span.storylink > a",
            "confidence": 0.9,
            "reasoning": "Class changed from titleline to storylink",
            "sample_values": ["First Article Title"],
        }
        with patch(
            "magpie.healer.selector_fixer._call_llm",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            html = (FIXTURES / "hackernews-v2-broken.html").read_text()
            result = await fix_selector(
                field_name="title",
                old_selector="span.titleline > a::text",
                html=html,
                old_samples=["First Article Title", "Second Article Title"],
            )
            assert result is not None
            assert result["selector"] == "span.storylink > a"
            assert result["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_llm_returns_null_selector(self) -> None:
        mock_response = {
            "selector": None,
            "confidence": 0.0,
            "reasoning": "Page structure changed too much",
            "sample_values": [],
        }
        with patch(
            "magpie.healer.selector_fixer._call_llm",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            html = (FIXTURES / "hackernews-v2-broken.html").read_text()
            result = await fix_selector(
                field_name="title",
                old_selector="span.titleline > a::text",
                html=html,
                old_samples=["First Article Title"],
            )
            assert result is not None
            assert result["selector"] is None

    @pytest.mark.asyncio
    async def test_llm_returns_invalid_json_retries(self) -> None:
        call_count = 0

        async def flaky_llm(*args: object, **kwargs: object) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Invalid JSON from LLM")
            return {
                "selector": "span.storylink > a",
                "confidence": 0.85,
                "reasoning": "Found after retry",
                "sample_values": [],
            }

        with patch(
            "magpie.healer.selector_fixer._call_llm",
            new_callable=AsyncMock,
            side_effect=flaky_llm,
        ):
            html = (FIXTURES / "hackernews-v2-broken.html").read_text()
            result = await fix_selector(
                field_name="title",
                old_selector="span.titleline > a::text",
                html=html,
                old_samples=[],
            )
            assert result is not None
            assert call_count == 3

    @pytest.mark.asyncio
    async def test_html_truncated_to_20k(self) -> None:
        large_html = "<html>" + "x" * 25000 + "</html>"
        captured_html = None

        async def capture_llm(*, html: str, **kwargs: object) -> dict:
            nonlocal captured_html
            captured_html = html
            return {
                "selector": "div",
                "confidence": 0.5,
                "reasoning": "test",
                "sample_values": [],
            }

        with patch(
            "magpie.healer.selector_fixer._call_llm",
            new_callable=AsyncMock,
            side_effect=capture_llm,
        ):
            await fix_selector(
                field_name="title",
                old_selector="div::text",
                html=large_html,
                old_samples=[],
            )
            assert captured_html is not None
            assert len(captured_html) <= 20000


class TestGitHubPR:
    """GitHub PR creation tests — mocked, no real API calls."""

    @pytest.mark.asyncio
    async def test_pr_created_with_correct_label(self) -> None:
        from magpie.healer.github_pr import create_heal_pr

        with patch(
            "magpie.healer.github_pr._github_api",
            new_callable=AsyncMock,
        ) as mock_api:
            mock_api.return_value = {"html_url": "https://github.com/test/repo/pull/1"}
            pr_url = await create_heal_pr(
                source_name="hackernews",
                field_name="title",
                old_selector="span.titleline > a::text",
                new_selector="span.storylink > a::text",
                confidence=0.9,
                reasoning="Class name changed",
                sample_values=["Article 1"],
            )
            assert pr_url is not None
            assert "pull" in pr_url

    @pytest.mark.asyncio
    async def test_existing_open_pr_updates_branch(self) -> None:
        from magpie.healer.github_pr import create_heal_pr

        with patch(
            "magpie.healer.github_pr._github_api",
            new_callable=AsyncMock,
        ) as mock_api:
            # Simulate finding an existing open PR
            mock_api.side_effect = [
                {"number": 5, "html_url": "https://github.com/test/repo/pull/5"},
                None,  # branch update
            ]
            pr_url = await create_heal_pr(
                source_name="hackernews",
                field_name="title",
                old_selector="span.old",
                new_selector="span.new",
                confidence=0.85,
                reasoning="Updated",
                sample_values=[],
            )
            assert pr_url is not None
