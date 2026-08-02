"""Smoke tier — cheap local sanity plus an env-gated probe of a live deployment.

See ``docs/specs/08-test-tiers.md``. The live probe only runs when
``MAGPIE_SMOKE_URL`` names an http(s) origin::

    MAGPIE_SMOKE_URL=https://magpie-backend-t4bb.onrender.com uv run pytest -m smoke

It is gated rather than unconditional because the deployment is on Render's free
tier and can be asleep or suspended; a red CI run for that reason says nothing
about the commit under test. It is *not* gated behind a broad ``try/except`` —
once you point it at a host, a 503 or a connection error is a failure.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
import pytest

SMOKE_URL_ENV = "MAGPIE_SMOKE_URL"
SMOKE_TIMEOUT_S = 20.0


class SmokeTargetError(RuntimeError):
    """``MAGPIE_SMOKE_URL`` is set but unusable.

    A separate outcome from "unset" on purpose: a typo'd variable must not be
    indistinguishable from a deliberately skipped tier.
    """


def smoke_base_url(raw: str | None) -> str | None:
    """Normalise the configured smoke target; ``None`` means the tier is off."""
    if raw is None or not raw.strip():
        return None
    url = raw.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        msg = f"{SMOKE_URL_ENV}={raw!r} is not an http(s) origin"
        raise SmokeTargetError(msg)
    return url


def require_smoke_target() -> str:
    """Return the smoke origin, or skip the calling test if none is configured."""
    target = smoke_base_url(os.environ.get(SMOKE_URL_ENV))
    if target is None:
        pytest.skip(f"{SMOKE_URL_ENV} unset — live smoke tier not configured")
    return target


# ── Local sanity (always runs) ───────────────────────────────────────────────


def test_package_imports() -> None:
    import magpie  # noqa: F401


def test_cli_entrypoint_exits_with_usage() -> None:
    """``magpie`` CLI with no args prints help and argparse exits with code 2."""
    from magpie.cli import main as magpie_main

    with pytest.raises(SystemExit) as exc_info:
        magpie_main([])
    assert exc_info.value.code == 2


# ── The gate itself (always runs) ────────────────────────────────────────────


class TestSmokeTargetResolution:
    @pytest.mark.parametrize("raw", [None, "", "   ", "\n"])
    def test_unset_or_blank_means_tier_off(self, raw: str | None) -> None:
        assert smoke_base_url(raw) is None

    def test_trailing_slash_is_stripped(self) -> None:
        assert smoke_base_url("https://example.test/") == "https://example.test"

    @pytest.mark.parametrize(
        "raw",
        [
            "example.test",  # no scheme — a very easy env-var typo
            "ftp://example.test",
            "https://",
            "not a url",
        ],
    )
    def test_malformed_target_fails_loudly(self, raw: str) -> None:
        with pytest.raises(SmokeTargetError):
            smoke_base_url(raw)

    def test_require_target_skips_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SMOKE_URL_ENV, raising=False)
        with pytest.raises(pytest.skip.Exception):
            require_smoke_target()

    def test_require_target_returns_configured_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SMOKE_URL_ENV, "https://example.test/")
        assert require_smoke_target() == "https://example.test"

    def test_require_target_does_not_skip_on_a_bad_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SMOKE_URL_ENV, "example.test")
        with pytest.raises(SmokeTargetError):
            require_smoke_target()


# ── Live deployment probe (skipped unless MAGPIE_SMOKE_URL is set) ───────────


async def _get(base: str, path: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=SMOKE_TIMEOUT_S, follow_redirects=True) as client:
        try:
            return await client.get(f"{base}{path}")
        except httpx.HTTPError as exc:
            pytest.fail(f"GET {base}{path} could not complete: {exc!r}")


@pytest.mark.smoke
async def test_deployed_health_is_ok() -> None:
    base = require_smoke_target()
    resp = await _get(base, "/health")
    assert resp.status_code == 200, f"GET {base}/health -> {resp.status_code}\n{resp.text[:500]}"
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "magpie"
    assert body["db"] == "ok", "deployment is up but its database is unreachable"
    assert body["commit_sha"], "/health reported no commit_sha"


@pytest.mark.smoke
async def test_deployed_version_agrees_with_health() -> None:
    base = require_smoke_target()
    version = await _get(base, "/version")
    assert version.status_code == 200, (
        f"GET {base}/version -> {version.status_code}\n{version.text[:500]}"
    )
    body = version.json()
    assert body["service"] == "magpie"
    assert body["commit_sha"] == body["version"]

    health = await _get(base, "/health")
    assert health.json()["commit_sha"] == body["commit_sha"]
