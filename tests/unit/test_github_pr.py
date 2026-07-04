"""Tests for healer GitHub PR creation (github_pr).

These drive the composed Git Data API flow — blob → tree → commit → ref → PR —
and assert that a commit carrying the patched config actually lands on the heal
branch before the PR is opened (the gap the previous implementation shipped).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magpie.healer.github_pr import _github_api, create_heal_pr


def _resp(status_code: int, json_data: object) -> httpx.Response:
    return httpx.Response(status_code, json=json_data, request=httpx.Request("GET", "http://test"))


class _Route:
    """Matches a request by HTTP method + a substring of the path."""

    def __init__(self, method: str, needle: str, response: httpx.Response) -> None:
        self.method = method
        self.needle = needle
        self.response = response


class _RoutingClient:
    """Fake httpx.AsyncClient that dispatches by (method, path) and records calls."""

    def __init__(self, routes: list[_Route]) -> None:
        self._routes = routes
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _RoutingClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def _dispatch(
        self, method: str, url: str, json: Any = None, params: Any = None
    ) -> httpx.Response:
        self.calls.append({"method": method, "url": url, "json": json, "params": params})
        for route in self._routes:
            if route.method == method and route.needle in url:
                return route.response
        raise AssertionError(f"no route for {method} {url}")

    async def get(self, url: str, headers: Any = None, params: Any = None) -> httpx.Response:
        return self._dispatch("GET", url, params=params)

    async def post(self, url: str, headers: Any = None, json: Any = None) -> httpx.Response:
        return self._dispatch("POST", url, json=json)

    async def patch(self, url: str, headers: Any = None, json: Any = None) -> httpx.Response:
        return self._dispatch("PATCH", url, json=json)

    def call(self, method: str, needle: str) -> dict[str, Any]:
        for entry in self.calls:
            if entry["method"] == method and needle in entry["url"]:
                return entry
        raise AssertionError(f"{method} {needle} was never called")

    def called(self, method: str, needle: str) -> bool:
        return any(e["method"] == method and needle in e["url"] for e in self.calls)


_PATCHED_YAML = "name: hackernews\nitem:\n  container: span.new\n"


def _git_data_routes() -> list[_Route]:
    """Successful Git Data API responses for a fresh branch."""
    return [
        _Route("GET", "/git/ref/heads/main", _resp(200, {"object": {"sha": "BASE_SHA"}})),
        _Route("GET", "/git/commits/BASE_SHA", _resp(200, {"tree": {"sha": "BASE_TREE"}})),
        _Route("POST", "/git/blobs", _resp(201, {"sha": "BLOB_SHA"})),
        _Route("POST", "/git/trees", _resp(201, {"sha": "TREE_SHA"})),
        _Route("POST", "/git/commits", _resp(201, {"sha": "COMMIT_SHA"})),
        _Route("POST", "/git/refs", _resp(201, {"ref": "refs/heads/heal/hackernews"})),
    ]


def _env() -> dict[str, str]:
    return {"GITHUB_PAT_SCRAPE_HEALER": "fake-token", "GITHUB_REPO": "owner/repo"}


async def _call_api(client: _RoutingClient, **overrides: Any) -> dict[str, Any] | None:
    kwargs: dict[str, Any] = {
        "source_name": "hackernews",
        "field_name": "title",
        "old_selector": "span.old::text",
        "new_selector": "span.new::text",
        "confidence": 0.9,
        "reasoning": "Class changed",
        "sample_values": ["Article 1"],
        "file_path": "configs/hackernews.yaml",
        "new_content": _PATCHED_YAML,
    }
    kwargs.update(overrides)
    with (
        patch("magpie.healer.github_pr.httpx.AsyncClient", return_value=client),
        patch.dict("os.environ", _env()),
    ):
        return await _github_api(**kwargs)


class TestCreateHealPr:
    async def test_returns_none_when_api_returns_none(self) -> None:
        with patch(
            "magpie.healer.github_pr._github_api",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await create_heal_pr(
                source_name="t",
                field_name="title",
                old_selector="old",
                new_selector="new",
                confidence=0.9,
                reasoning="r",
                sample_values=[],
                file_path="configs/t.yaml",
                new_content="name: t\n",
            )
            assert result is None


class TestGitDataFlow:
    async def test_creates_branch_commit_and_pr(self) -> None:
        routes = [
            *_git_data_routes(),
            _Route("GET", "/pulls", _resp(200, [])),
            _Route(
                "POST",
                "/pulls",
                _resp(201, {"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}),
            ),
            _Route("POST", "/issues/42/labels", _resp(200, [{"name": "scrape:self-heal"}])),
        ]
        client = _RoutingClient(routes)
        result = await _call_api(client)

        assert result is not None
        assert result["html_url"] == "https://github.com/owner/repo/pull/42"

        # A blob carrying the *patched* config content was created.
        blob = client.call("POST", "/git/blobs")
        assert blob["json"]["content"] == _PATCHED_YAML
        assert blob["json"]["encoding"] == "utf-8"

        # The tree swaps the file in on top of the base tree.
        tree = client.call("POST", "/git/trees")
        assert tree["json"]["base_tree"] == "BASE_TREE"
        assert tree["json"]["tree"][0]["path"] == "configs/hackernews.yaml"
        assert tree["json"]["tree"][0]["sha"] == "BLOB_SHA"

        # A commit parented on base, pointing at the new tree, lands on the ref.
        commit = client.call("POST", "/git/commits")
        assert commit["json"]["tree"] == "TREE_SHA"
        assert commit["json"]["parents"] == ["BASE_SHA"]

        ref = client.call("POST", "/git/refs")
        assert ref["json"]["ref"] == "refs/heads/heal/hackernews"
        assert ref["json"]["sha"] == "COMMIT_SHA"

    async def test_existing_branch_updates_ref_on_422(self) -> None:
        routes = [
            _Route("GET", "/git/ref/heads/main", _resp(200, {"object": {"sha": "BASE_SHA"}})),
            _Route("GET", "/git/commits/BASE_SHA", _resp(200, {"tree": {"sha": "BASE_TREE"}})),
            _Route("POST", "/git/blobs", _resp(201, {"sha": "BLOB_SHA"})),
            _Route("POST", "/git/trees", _resp(201, {"sha": "TREE_SHA"})),
            _Route("POST", "/git/commits", _resp(201, {"sha": "COMMIT_SHA"})),
            # Branch already exists -> POST refs 422, PATCH fast-forwards it.
            _Route("POST", "/git/refs", _resp(422, {"message": "Reference already exists"})),
            _Route("PATCH", "/git/refs/heads/heal/hackernews", _resp(200, {"ref": "ok"})),
            _Route("GET", "/pulls", _resp(200, [])),
            _Route(
                "POST",
                "/pulls",
                _resp(201, {"number": 7, "html_url": "https://gh/owner/repo/pull/7"}),
            ),
            _Route("POST", "/issues/7/labels", _resp(200, [])),
        ]
        client = _RoutingClient(routes)
        result = await _call_api(client)

        assert result is not None
        patch_ref = client.call("PATCH", "/git/refs/heads/heal/hackernews")
        assert patch_ref["json"]["sha"] == "COMMIT_SHA"
        assert patch_ref["json"]["force"] is True

    async def test_updates_existing_pr_but_still_commits(self) -> None:
        existing = {"number": 5, "html_url": "https://github.com/owner/repo/pull/5"}
        routes = [
            *_git_data_routes(),
            _Route("GET", "/pulls", _resp(200, [existing])),
            _Route("PATCH", "/pulls/5", _resp(200, existing)),
            _Route("POST", "/issues/5/labels", _resp(200, [])),
        ]
        client = _RoutingClient(routes)
        result = await _call_api(client, field_name="url")

        assert result is not None
        assert result["number"] == 5
        # The commit still landed even though we reused the open PR.
        assert client.called("POST", "/git/commits")
        pr_patch = client.call("PATCH", "/pulls/5")
        assert pr_patch["json"]["title"] == "heal(hackernews): update url selector"
        # No new PR was created.
        assert not client.called("POST", "/repos/owner/repo/pulls")

    async def test_no_pr_when_base_ref_missing(self) -> None:
        routes = [
            _Route("GET", "/git/ref/heads/main", _resp(404, {"message": "Not Found"})),
        ]
        client = _RoutingClient(routes)
        result = await _call_api(client)

        assert result is None
        # Bailed out before touching the pulls API.
        assert not client.called("POST", "/git/blobs")
        assert not client.called("POST", "/pulls")

    async def test_no_pr_when_blob_creation_fails(self) -> None:
        routes = [
            _Route("GET", "/git/ref/heads/main", _resp(200, {"object": {"sha": "BASE_SHA"}})),
            _Route("GET", "/git/commits/BASE_SHA", _resp(200, {"tree": {"sha": "BASE_TREE"}})),
            _Route("POST", "/git/blobs", _resp(403, {"message": "bad token"})),
        ]
        client = _RoutingClient(routes)
        result = await _call_api(client)
        assert result is None
        assert not client.called("POST", "/pulls")

    async def test_respects_custom_base_branch(self) -> None:
        routes = [
            _Route("GET", "/git/ref/heads/develop", _resp(200, {"object": {"sha": "BASE_SHA"}})),
            _Route("GET", "/git/commits/BASE_SHA", _resp(200, {"tree": {"sha": "BASE_TREE"}})),
            _Route("POST", "/git/blobs", _resp(201, {"sha": "BLOB_SHA"})),
            _Route("POST", "/git/trees", _resp(201, {"sha": "TREE_SHA"})),
            _Route("POST", "/git/commits", _resp(201, {"sha": "COMMIT_SHA"})),
            _Route("POST", "/git/refs", _resp(201, {"ref": "ok"})),
            _Route("GET", "/pulls", _resp(200, [])),
            _Route("POST", "/pulls", _resp(201, {"number": 1, "html_url": "https://gh/1"})),
            _Route("POST", "/issues/1/labels", _resp(200, [])),
        ]
        client = _RoutingClient(routes)
        with (
            patch("magpie.healer.github_pr.httpx.AsyncClient", return_value=client),
            patch.dict("os.environ", {**_env(), "GITHUB_BASE_BRANCH": "develop"}),
        ):
            result = await _github_api(
                source_name="hackernews",
                field_name="title",
                old_selector="old",
                new_selector="new",
                confidence=0.9,
                reasoning="r",
                sample_values=[],
                file_path="configs/hackernews.yaml",
                new_content=_PATCHED_YAML,
            )
        assert result is not None
        pr = client.call("POST", "/pulls")
        assert pr["json"]["base"] == "develop"


@pytest.mark.asyncio
async def test_head_filter_uses_owner_branch_format() -> None:
    """GitHub's ``head`` param requires ``owner:branch``; a bare branch matches nothing."""
    routes = [
        *_git_data_routes(),
        _Route("GET", "/pulls", _resp(200, [])),
        _Route("POST", "/pulls", _resp(201, {"number": 1, "html_url": "https://gh/1"})),
        _Route("POST", "/issues/1/labels", _resp(200, [])),
    ]
    client = _RoutingClient(routes)
    with (
        patch("magpie.healer.github_pr.httpx.AsyncClient", return_value=client),
        patch.dict(
            "os.environ", {"GITHUB_PAT_SCRAPE_HEALER": "t", "GITHUB_REPO": "octocat/magpie"}
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
            file_path="configs/hackernews.yaml",
            new_content=_PATCHED_YAML,
        )
    get_pulls = client.call("GET", "/pulls")
    assert get_pulls["params"]["head"] == "octocat:heal/hackernews"
