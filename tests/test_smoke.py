"""Smoke tier — cheap local sanity plus an env-gated probe of a live deployment.

See ``docs/specs/08-test-tiers.md``. The live probe only runs when
``SMOKE_BASE_URL`` names an http(s) origin — a *bare* origin, no trailing slash
and no path; the tests append ``/health`` and ``/version`` themselves::

    SMOKE_BASE_URL=https://magpie-backend-t4bb.onrender.com uv run pytest -m smoke

CI passes it as ``${{ vars.SMOKE_BASE_URL }}`` in the push-gated ``smoke`` job
only, so the main test job keeps skipping this tier.

It is gated rather than unconditional because the deployment is on Render's free
tier and can be asleep or suspended; a red CI run for that reason says nothing
about the commit under test. It is *not* gated behind a broad ``try/except`` —
once you point it at a host, exhausting the retry budget is a failure.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

import httpx
import pytest

SMOKE_URL_ENV = "SMOKE_BASE_URL"

# Render Free spins an instance down when idle. The first request after that was
# measured holding the connection open for ~70s while the container cold-booted,
# so a tight timeout reports "deploy broken" for what is really a cold start.
SMOKE_TIMEOUT_S = 120.0
SMOKE_ATTEMPTS = 3
# Slept only after a retryable 5xx *response* — Render's proxy can answer 502
# while the container is still coming up. Transport errors retry immediately.
SMOKE_RETRY_DELAY_S = 5.0
_RETRY_STATUS = frozenset({502, 503, 504})


class SmokeTargetError(RuntimeError):
    """``SMOKE_BASE_URL`` is set but unusable.

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


# ── Live deployment probe (skipped unless SMOKE_BASE_URL is set) ─────────────


async def _get(base: str, path: str) -> httpx.Response:
    """GET ``{base}{path}``, tolerating a cold-booting free-tier instance.

    ``base`` is a bare origin and ``path`` starts with ``/``, so the two join
    with exactly one separator; ``base`` is rstripped anyway so a stray trailing
    slash cannot produce ``//health``.

    Retries transport errors and 502/503/504 up to :data:`SMOKE_ATTEMPTS` times.
    What is *not* tolerated is exhausting that budget: a transport error then
    fails the test with the URL echoed, and a lingering 5xx is handed back to
    the caller so its own status assertion fails with the body echoed. A
    suspended or DB-less deployment is a real failure once you asked for a
    smoke run.
    """
    url = f"{base.rstrip('/')}{path}"
    last_exc: httpx.HTTPError | None = None
    response: httpx.Response | None = None

    async with httpx.AsyncClient(timeout=SMOKE_TIMEOUT_S, follow_redirects=True) as client:
        for attempt in range(1, SMOKE_ATTEMPTS + 1):
            if attempt > 1 and response is not None:
                await asyncio.sleep(SMOKE_RETRY_DELAY_S)
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                last_exc, response = exc, None
                continue
            if response.status_code not in _RETRY_STATUS:
                return response

    if response is not None:
        return response
    pytest.fail(f"GET {url} could not complete: {last_exc!r}")


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
