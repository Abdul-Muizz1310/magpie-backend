"""The tier gates themselves (spec 08 failure cases).

Both gates decide whether a whole tier runs, so a bug in either turns a real
check into a silent no-op. They are cheap pure functions — tested here in the
fast tier, without Docker and without a deployment.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration.docker_gate import docker_skip_reason
from tests.test_smoke import SMOKE_URL_ENV, _get, require_smoke_target


class TestDockerGate:
    def test_unreachable_daemon_yields_a_reason_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An absent daemon must skip loudly, never read as a passing tier."""

        class _Boom:
            def __init__(self) -> None:
                raise ConnectionRefusedError("docker daemon is not running")

        monkeypatch.setattr("testcontainers.core.docker_client.DockerClient", _Boom, raising=True)
        reason = docker_skip_reason()
        assert reason is not None
        assert "Docker daemon unavailable" in reason
        assert "ConnectionRefusedError" in reason

    def test_reachable_daemon_yields_no_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Ok:
            def __init__(self) -> None:
                self.client = self

            def ping(self) -> bool:
                return True

        monkeypatch.setattr("testcontainers.core.docker_client.DockerClient", _Ok, raising=True)
        assert docker_skip_reason() is None


class TestSmokeGateFailurePaths:
    async def test_connection_error_fails_with_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead host must fail the smoke test, not be swallowed into a pass."""

        async def _boom(self: object, url: str, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx.AsyncClient, "get", _boom, raising=True)
        with pytest.raises(pytest.fail.Exception) as excinfo:
            await _get("https://example.test", "/health")
        assert "https://example.test/health" in str(excinfo.value)
        assert "ConnectError" in str(excinfo.value)

    async def test_timeout_also_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _slow(self: object, url: str, **kwargs: object) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        monkeypatch.setattr(httpx.AsyncClient, "get", _slow, raising=True)
        with pytest.raises(pytest.fail.Exception):
            await _get("https://example.test", "/version")

    def test_a_typoed_url_is_never_mistaken_for_an_unconfigured_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Also pins the env-var name CI wires up.

        The literal below is what ``.github/workflows/ci.yml``'s ``smoke`` job
        passes as ``${{ vars.SMOKE_BASE_URL }}``. If ``SMOKE_URL_ENV`` were
        renamed without updating the workflow, this ``setenv`` would land on a
        variable nothing reads: ``require_smoke_target()`` would see an unset
        tier and *skip*, and the final assertion catches exactly that. The
        equality check makes the coupling explicit rather than incidental.
        """
        assert SMOKE_URL_ENV == "SMOKE_BASE_URL", (
            "the smoke tier's env var is wired into ci.yml as vars.SMOKE_BASE_URL"
        )
        monkeypatch.setenv("SMOKE_BASE_URL", "https:/magpie.example.test")
        with pytest.raises(Exception) as excinfo:
            require_smoke_target()
        assert not isinstance(excinfo.value, pytest.skip.Exception)
