"""Commit-SHA resolution for ``/health`` and ``/version``.

The deployed image previously always reported ``"unknown"``: the Dockerfile
declares ``ARG COMMIT_SHA=unknown`` and promotes it to ``ENV``, so *every* image
built without ``--build-arg COMMIT_SHA=...`` ships with ``COMMIT_SHA=unknown``
literally set in the environment. A naive ``os.environ.get("COMMIT_SHA", "dev")``
therefore can never fall back to anything, and Render's own
``RENDER_GIT_COMMIT`` was ignored.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from magpie.main import app
from magpie.platform.health import (
    _SHA_ENV_VARS,
    PLACEHOLDER_SHAS,
    UNKNOWN_SHA,
    commit_sha,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
RENDER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


@pytest.fixture(autouse=True)
def _clear_sha_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an environment with no SHA source set.

    Derived from ``_SHA_ENV_VARS`` rather than an explicit list: this fixture
    previously cleared only COMMIT_SHA and RENDER_GIT_COMMIT while the resolver
    also consults GITHUB_SHA, which GitHub Actions always sets. The
    "nothing configured" cases therefore passed locally and failed in CI,
    resolving to the runner's commit instead of "unknown". Iterating the
    resolver's own tuple means a new source cannot silently reintroduce that.
    """
    for name in _SHA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestCommitShaResolution:
    def test_explicit_build_arg_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMMIT_SHA", SHA)
        monkeypatch.setenv("RENDER_GIT_COMMIT", RENDER_SHA)
        assert commit_sha() == SHA

    def test_falls_back_to_render_git_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RENDER_GIT_COMMIT", RENDER_SHA)
        assert commit_sha() == RENDER_SHA

    @pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_SHAS))
    def test_dockerfile_placeholder_does_not_shadow_render_sha(
        self, monkeypatch: pytest.MonkeyPatch, placeholder: str
    ) -> None:
        """The bug: ``COMMIT_SHA=unknown`` is *set*, so ``.get(..., default)`` never fires."""
        monkeypatch.setenv("COMMIT_SHA", placeholder)
        monkeypatch.setenv("RENDER_GIT_COMMIT", RENDER_SHA)
        assert commit_sha() == RENDER_SHA

    def test_blank_commit_sha_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMMIT_SHA", "   ")
        monkeypatch.setenv("RENDER_GIT_COMMIT", RENDER_SHA)
        assert commit_sha() == RENDER_SHA

    def test_values_are_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMMIT_SHA", f"  {SHA}\n")
        assert commit_sha() == SHA

    def test_nothing_configured_reports_unknown(self) -> None:
        assert commit_sha() == UNKNOWN_SHA

    def test_placeholder_only_still_reports_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMMIT_SHA", "unknown")
        assert commit_sha() == UNKNOWN_SHA


class TestVersionEndpointReportsResolvedSha:
    async def test_version_uses_render_git_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMMIT_SHA", "unknown")
        monkeypatch.setenv("RENDER_GIT_COMMIT", RENDER_SHA)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/version")
        assert resp.status_code == 200
        body = resp.json()
        assert body["commit_sha"] == RENDER_SHA
        assert body["version"] == RENDER_SHA

    async def test_health_uses_resolved_sha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMMIT_SHA", SHA)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health")
        assert resp.json()["commit_sha"] == SHA
