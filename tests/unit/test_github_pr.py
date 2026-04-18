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
        """No existing open PRs -> creates a new one and applies the label."""
        mock_client = AsyncMock()
        mock_client.get.return_value = _mock_response(200, [])
        mock_client.post.side_effect = [
            _mock_response(
                201, {"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}
            ),
            _mock_response(200, [{"name": "scrape:self-heal"}]),
        ]

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
        assert result["html_url"] == "https://github.com/owner/repo/pull/42"

    @pytest.mark.asyncio
    async def test_head_filter_uses_owner_branch_format(self) -> None:
        """GitHub's ``head`` param requires ``owner:branch``; a bare branch silently
        matches nothing and would cause duplicate PRs."""
        mock_client = AsyncMock()
        mock_client.get.return_value = _mock_response(200, [])
        mock_client.post.side_effect = [
            _mock_response(201, {"number": 1, "html_url": "https://gh/owner/repo/pull/1"}),
            _mock_response(200, []),
        ]

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("magpie.healer.github_pr.httpx.AsyncClient", return_value=mock_ctx),
            patch.dict(
                "os.environ",
                {"GITHUB_PAT_SCRAPE_HEALER": "fake-token", "GITHUB_REPO": "octocat/magpie"},
            ),
        ):
            await _github_api(
                source_name="hackernews",
                field_name="title",
                old_selector="old",
                new_selector="new",
                confidence=0.9,
                reasoning="r",
                sample_values=[],
            )

        get_kwargs = mock_client.get.call_args.kwargs
        assert get_kwargs["params"]["head"] == "octocat:heal/hackernews"

    @pytest.mark.asyncio
    async def test_new_pr_applies_label_via_issues_endpoint(self) -> None:
        """Label attachment requires a separate call to /issues/{n}/labels."""
        mock_client = AsyncMock()
        mock_client.get.return_value = _mock_response(200, [])
        mock_client.post.side_effect = [
            _mock_response(201, {"number": 7, "html_url": "https://gh/owner/repo/pull/7"}),
            _mock_response(200, [{"name": "scrape:self-heal"}]),
        ]

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
            await _github_api(
                source_name="hackernews",
                field_name="title",
                old_selector="old",
                new_selector="new",
                confidence=0.9,
                reasoning="r",
                sample_values=[],
            )

        # Two POSTs: one to create the PR, one to apply the label.
        assert mock_client.post.call_count == 2
        label_call = mock_client.post.call_args_list[1]
        assert label_call.args[0] == "/repos/owner/repo/issues/7/labels"
        assert label_call.kwargs["json"] == {"labels": ["scrape:self-heal"]}

    @pytest.mark.asyncio
    async def test_updates_existing_pr_title_and_body(self) -> None:
        """Existing open PR -> PATCH with both title and body (an earlier heal may
        have been for a different field)."""
        existing_pr = {"number": 5, "html_url": "https://github.com/owner/repo/pull/5"}

        mock_client = AsyncMock()
        mock_client.get.return_value = _mock_response(200, [existing_pr])
        mock_client.patch.return_value = _mock_response(200, existing_pr)
        mock_client.post.return_value = _mock_response(200, [{"name": "scrape:self-heal"}])

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
                field_name="url",
                old_selector="span.old::text",
                new_selector="span.new::text",
                confidence=0.85,
                reasoning="Updated",
                sample_values=["Sample"],
            )

        assert result is not None
        assert result["number"] == 5
        patch_kwargs = mock_client.patch.call_args.kwargs
        assert patch_kwargs["json"]["title"] == "heal(hackernews): update url selector"
        assert "span.new::text" in patch_kwargs["json"]["body"]

    @pytest.mark.asyncio
    async def test_updates_existing_pr_also_reapplies_label(self) -> None:
        """When updating an existing PR, label application runs too — idempotent."""
        existing_pr = {"number": 5, "html_url": "https://github.com/owner/repo/pull/5"}

        mock_client = AsyncMock()
        mock_client.get.return_value = _mock_response(200, [existing_pr])
        mock_client.patch.return_value = _mock_response(200, existing_pr)
        mock_client.post.return_value = _mock_response(200, [{"name": "scrape:self-heal"}])

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
            await _github_api(
                source_name="hackernews",
                field_name="title",
                old_selector="span.old",
                new_selector="span.new",
                confidence=0.85,
                reasoning="Updated",
                sample_values=[],
            )

        mock_client.patch.assert_called_once()
        # POST was only for labels (no create call, because we updated)
        assert mock_client.post.call_count == 1
        label_call = mock_client.post.call_args
        assert label_call.args[0] == "/repos/owner/repo/issues/5/labels"

    @pytest.mark.asyncio
    async def test_returns_none_on_get_failure(self) -> None:
        """GET pulls returns non-200 and POST also fails."""
        mock_client = AsyncMock()
        mock_client.get.return_value = _mock_response(403, {"message": "forbidden"})
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
        mock_client.post.side_effect = [
            _mock_response(
                200, {"number": 10, "html_url": "https://github.com/owner/repo/pull/10"}
            ),
            _mock_response(200, [{"name": "scrape:self-heal"}]),
        ]

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
