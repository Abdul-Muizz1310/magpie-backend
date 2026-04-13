"""Tests for healer GitHub PR creation (github_pr._github_api)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magpie.healer.github_pr import _github_api, create_heal_pr


def _mock_response(status_code: int, json_data: object) -> httpx.Response:
    resp = httpx.Response(status_code, json=json_data, request=httpx.Request("GET", "http://test"))
    return resp


class TestCreateHealPr:
    @pytest.mark.asyncio
    async def test_returns_none_when_api_returns_none(self) -> None:
        with patch(
            "magpie.healer.github_pr._github_api",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await create_heal_pr(
                source_name="test",
                field_name="title",
                old_selector="old",
                new_selector="new",
                confidence=0.9,
                reasoning="test",
                sample_values=[],
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_html_url(self) -> None:
        with patch(
            "magpie.healer.github_pr._github_api",
            new_callable=AsyncMock,
            return_value={"number": 1},
        ):
            result = await create_heal_pr(
                source_name="test",
                field_name="title",
                old_selector="old",
                new_selector="new",
                confidence=0.9,
                reasoning="test",
                sample_values=[],
            )
            assert result is None


class TestGitHubApi:
    @pytest.mark.asyncio
    async def test_creates_new_pr_when_no_existing(self) -> None:
        """No existing open PRs -> creates a new one."""
        mock_client = AsyncMock()
        # First call: GET pulls returns empty list
        mock_client.get.return_value = _mock_response(200, [])
        # Second call: POST pulls creates a new PR
        mock_client.post.return_value = _mock_response(
            201, {"number": 42, "html_url": "https://github.com/test/repo/pull/42"}
        )

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("magpie.healer.github_pr.httpx.AsyncClient", return_value=mock_ctx),
            patch.dict(
                "os.environ",
                {"GITHUB_PAT_SCRAPE_HEALER": "fake-token", "GITHUB_REPO": "owner/repo"},
            ),
        ):
            result = await _github_api(
                source_name="hackernews",
                field_name="title",
                old_selector="span.old::text",
                new_selector="span.new::text",
                confidence=0.9,
                reasoning="Class changed",
                sample_values=["Article 1", "Article 2"],
            )

        assert result is not None
        assert result["html_url"] == "https://github.com/test/repo/pull/42"

    @pytest.mark.asyncio
    async def test_updates_existing_pr(self) -> None:
        """Existing open PR found -> updates it."""
        existing_pr = {"number": 5, "html_url": "https://github.com/test/repo/pull/5"}

        mock_client = AsyncMock()
        # GET returns existing PR
        mock_client.get.return_value = _mock_response(200, [existing_pr])
        # PATCH to update
        mock_client.patch.return_value = _mock_response(200, existing_pr)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("magpie.healer.github_pr.httpx.AsyncClient", return_value=mock_ctx),
            patch.dict("os.environ", {"GITHUB_PAT_SCRAPE_HEALER": "fake-token"}),
        ):
            result = await _github_api(
                source_name="hackernews",
                field_name="title",
                old_selector="span.old::text",
                new_selector="span.new::text",
                confidence=0.85,
                reasoning="Updated",
                sample_values=["Sample"],
            )

        assert result is not None
        assert result["number"] == 5
        mock_client.patch.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_on_get_failure(self) -> None:
        """GET pulls returns non-200 and POST also fails."""
        mock_client = AsyncMock()
        # GET returns 403
        mock_client.get.return_value = _mock_response(403, {"message": "forbidden"})
        # POST returns 403
        mock_client.post.return_value = _mock_response(403, {"message": "forbidden"})

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("magpie.healer.github_pr.httpx.AsyncClient", return_value=mock_ctx),
            patch.dict("os.environ", {"GITHUB_PAT_SCRAPE_HEALER": "fake-token"}),
        ):
            result = await _github_api(
                source_name="test",
                field_name="title",
                old_selector="old",
                new_selector="new",
                confidence=0.5,
                reasoning="test",
                sample_values=[],
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_post_returns_200_accepted(self) -> None:
        """POST returns 200 (instead of 201) — still accepted."""
        mock_client = AsyncMock()
        mock_client.get.return_value = _mock_response(200, [])
        mock_client.post.return_value = _mock_response(
            200, {"number": 10, "html_url": "https://github.com/test/repo/pull/10"}
        )

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("magpie.healer.github_pr.httpx.AsyncClient", return_value=mock_ctx),
            patch.dict("os.environ", {"GITHUB_PAT_SCRAPE_HEALER": "fake-token"}),
        ):
            result = await _github_api(
                source_name="test",
                field_name="title",
                old_selector="old",
                new_selector="new",
                confidence=0.9,
                reasoning="test",
                sample_values=[],
            )

        assert result is not None
        assert result["number"] == 10
