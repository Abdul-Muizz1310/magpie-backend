"""Platform token contract v2 — conformance tests 1 to 14 plus magpie's extras (spec 10).

The workspace contract (``docs/specs/platform-contract-v2.md``) is the source; each
backend copies its §3 tests. Tokens here are minted the way bastion and
magpie-frontend mint them, with throwaway Ed25519 keys.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from magpie.main import create_app
from magpie.platform import platform_token
from magpie.platform.platform_token import (
    PlatformAuthSettings,
    install_platform_token,
)
from magpie.platform.principal import Principal, principal_of
from magpie.platform.rate_limit import bucket_key

SERVICE = "magpie"
BASTION_ENV = "BASTION_SIGNING_KEY_PUBLIC"
FRONTEND_ENV = "MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC"
_DROP = object()


@dataclass(frozen=True)
class _Key:
    priv_pem: str
    pub_b64: str


def _keypair() -> _Key:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    der = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return _Key(pem, base64.b64encode(der).decode())


def _bastion_claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "bastion",
        "aud": SERVICE,
        "service": SERVICE,
        "sub": "v_0123456789",
        "role": "viewer",
        "cap": "magpie.read",
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not _DROP}


def _frontend_claims(**overrides: object) -> dict[str, object]:
    base = _bastion_claims(iss="magpie-frontend", sub="magpie-frontend", role="frontend")
    del base["cap"]
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not _DROP}


def _sign(key: _Key, claims: dict[str, object], *, kid: str = "test-kid") -> str:
    return jwt.encode(claims, key.priv_pem, algorithm="EdDSA", headers={"kid": kid})


def _b64url(data: dict[str, object]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def bastion() -> _Key:
    return _keypair()


@pytest.fixture
def frontend() -> _Key:
    return _keypair()


@pytest.fixture(autouse=True)
def _keys_env(monkeypatch: pytest.MonkeyPatch, bastion: _Key, frontend: _Key) -> Iterator[None]:
    """Both trusted keys configured; individual tests remove them as needed."""
    monkeypatch.delenv("BASTION_PUBLIC_KEY_URL", raising=False)
    monkeypatch.setenv(BASTION_ENV, bastion.pub_b64)
    monkeypatch.setenv(FRONTEND_ENV, frontend.pub_b64)
    monkeypatch.setattr(platform_token.logger, "disabled", False)
    platform_token.reset_public_key_cache()
    yield
    platform_token.reset_public_key_cache()


def _app(*, demo_mode: bool = False, test_settings: bool = False) -> TestClient:
    app = FastAPI()
    install_platform_token(
        app, settings=PlatformAuthSettings(demo_mode=demo_mode, test_settings=test_settings)
    )

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/protected")
    async def _protected(request: Request) -> dict[str, str | None]:
        principal = principal_of(request)
        return {
            "iss": principal.iss if principal else None,
            "sub": principal.sub if principal else None,
        }

    return TestClient(app)


def _get(client: TestClient, token: str | None = None) -> tuple[int, dict[str, object]]:
    headers = {"X-Platform-Token": token} if token is not None else {}
    resp = client.get("/protected", headers=headers)
    return resp.status_code, resp.json()


INVALID = (401, {"detail": "invalid_platform_token"})
MISSING = (401, {"detail": "missing_platform_token"})


# ── §3 conformance tests ─────────────────────────────────────────────────────


class TestConformance:
    def test_01_valid_bastion_token_is_authenticated(self, bastion: _Key) -> None:
        status, body = _get(_app(), _sign(bastion, _bastion_claims()))
        assert status == 200
        assert body == {"iss": "bastion", "sub": "v_0123456789"}

    @pytest.mark.parametrize("other", ["paper-trail", "inkprint", "inkthread", "slowquery"])
    def test_02_aud_naming_another_service_is_invalid(self, bastion: _Key, other: str) -> None:
        assert _get(_app(), _sign(bastion, _bastion_claims(aud=other))) == INVALID

    def test_03_missing_aud_is_invalid(self, bastion: _Key) -> None:
        assert _get(_app(), _sign(bastion, _bastion_claims(aud=_DROP))) == INVALID

    @pytest.mark.parametrize("aud", [[SERVICE], [SERVICE, "paper-trail"], []])
    def test_03_list_aud_is_invalid(self, bastion: _Key, aud: list[str]) -> None:
        assert _get(_app(), _sign(bastion, _bastion_claims(aud=aud))) == INVALID

    def test_04_bastion_iss_signed_with_frontend_key_is_invalid(self, frontend: _Key) -> None:
        forged = _sign(frontend, _bastion_claims())
        assert _get(_app(), forged) == INVALID

    def test_04_frontend_iss_signed_with_bastion_key_is_invalid(self, bastion: _Key) -> None:
        """The key decides the issuer, in both directions."""
        assert _get(_app(), _sign(bastion, _frontend_claims())) == INVALID

    @pytest.mark.parametrize("role", ["admin", "viewer", "editor", "", None])
    def test_05_frontend_key_with_non_frontend_role_is_invalid(
        self, frontend: _Key, role: str | None
    ) -> None:
        claims = _frontend_claims(role=role) if role is not None else _frontend_claims(role=_DROP)
        assert _get(_app(), _sign(frontend, claims)) == INVALID

    def test_06_expired_5s_ago_is_within_leeway(self, bastion: _Key) -> None:
        now = int(time.time())
        token = _sign(bastion, _bastion_claims(iat=now - 65, exp=now - 5))
        assert _get(_app(), token)[0] == 200

    def test_06_expired_15s_ago_is_invalid(self, bastion: _Key) -> None:
        now = int(time.time())
        token = _sign(bastion, _bastion_claims(iat=now - 75, exp=now - 15))
        assert _get(_app(), token) == INVALID

    def test_07_iat_5s_ahead_is_within_leeway(self, bastion: _Key) -> None:
        now = int(time.time())
        token = _sign(bastion, _bastion_claims(iat=now + 5, exp=now + 65))
        assert _get(_app(), token)[0] == 200

    def test_07_iat_15s_ahead_is_invalid(self, bastion: _Key) -> None:
        now = int(time.time())
        token = _sign(bastion, _bastion_claims(iat=now + 15, exp=now + 75))
        assert _get(_app(), token) == INVALID

    def test_08_alg_none_is_invalid(self) -> None:
        unsigned = f"{_b64url({'alg': 'none', 'typ': 'JWT'})}.{_b64url(_bastion_claims())}."
        assert _get(_app(), unsigned) == INVALID

    def test_08_hs256_is_invalid(self, bastion: _Key) -> None:
        """HS256 keyed with the *public* key — the classic algorithm-confusion forgery."""
        hs = jwt.encode(_bastion_claims(), bastion.pub_b64, algorithm="HS256")
        assert _get(_app(), hs) == INVALID

    def test_09_tampered_payload_is_invalid(self, bastion: _Key) -> None:
        header, _, signature = _sign(bastion, _bastion_claims()).split(".")
        forged = _b64url(_bastion_claims(sub="v_attacker00", role="admin"))
        assert _get(_app(), f"{header}.{forged}.{signature}") == INVALID

    def test_10_no_token_without_demo_mode_is_missing(self) -> None:
        assert _get(_app(demo_mode=False)) == MISSING

    def test_10_no_token_with_demo_mode_proceeds_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=platform_token.__name__)
        status, body = _get(_app(demo_mode=True))
        assert status == 200
        assert body == {"iss": None, "sub": None}
        assert any(
            r.levelno == logging.WARNING and "DEMO_MODE" in r.getMessage() for r in caplog.records
        )

    def test_11_invalid_token_with_demo_mode_is_still_invalid(self, bastion: _Key) -> None:
        token = _sign(bastion, _bastion_claims(aud="paper-trail"))
        assert _get(_app(demo_mode=True), token) == INVALID

    def test_12_no_trusted_key_is_503_on_protected_and_200_on_health(
        self, monkeypatch: pytest.MonkeyPatch, bastion: _Key
    ) -> None:
        monkeypatch.delenv(BASTION_ENV)
        monkeypatch.delenv(FRONTEND_ENV)
        for demo_mode in (False, True):
            client = _app(demo_mode=demo_mode, test_settings=False)
            assert _get(client) == (503, {"detail": "platform_auth_unconfigured"})
            assert _get(client, _sign(bastion, _bastion_claims()))[0] == 503
            assert client.get("/health").status_code == 200

    def test_13_every_public_route_answers_without_a_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On the real app, with enforcement on and no token at all."""

        # /health probes the DB; stub it so this test never builds the shared
        # asyncpg engine on TestClient's event loop (a later test on another loop
        # would then read the database as down).
        async def _db_ok() -> bool:
            return True

        monkeypatch.setattr("magpie.storage.db.check_db", _db_ok)
        client = TestClient(create_app(auth=PlatformAuthSettings(False, False)))
        for path in ("/health", "/version", "/metrics", "/openapi.json", "/docs", "/redoc"):
            resp = client.get(path)
            assert resp.status_code not in (401, 503), f"{path} -> {resp.status_code}"
        # …and a protected route really is protected on that app.
        assert client.get("/sources").json() == {"detail": "missing_platform_token"}

    def test_14_rate_limit_key_is_iss_colon_sub_when_authenticated(self, bastion: _Key) -> None:
        app = FastAPI()
        seen: list[str] = []

        @app.get("/protected")
        async def _protected(request: Request) -> dict[str, str]:
            seen.append(bucket_key(request))
            return {}

        install_platform_token(app, settings=PlatformAuthSettings(False, False))
        client = TestClient(app)
        assert _get(client, _sign(bastion, _bastion_claims(sub="v_visitor001")))[0] == 200
        assert seen == ["bastion:v_visitor001"]


# ── magpie extras (spec 10) ──────────────────────────────────────────────────


class TestFrontendToken:
    def test_valid_frontend_token_is_authenticated(self, frontend: _Key) -> None:
        status, body = _get(_app(), _sign(frontend, _frontend_claims()))
        assert status == 200
        assert body == {"iss": "magpie-frontend", "sub": "magpie-frontend"}

    def test_frontend_token_for_another_audience_is_invalid(self, frontend: _Key) -> None:
        assert _get(_app(), _sign(frontend, _frontend_claims(aud="paper-trail"))) == INVALID

    def test_only_frontend_key_configured_rejects_bastion_tokens(
        self, monkeypatch: pytest.MonkeyPatch, bastion: _Key, frontend: _Key
    ) -> None:
        monkeypatch.delenv(BASTION_ENV)
        client = _app()
        assert _get(client, _sign(frontend, _frontend_claims()))[0] == 200
        assert _get(client, _sign(bastion, _bastion_claims())) == INVALID
        assert _get(client) == MISSING


class TestClaimShape:
    def test_lifetime_of_300s_is_accepted_and_301_rejected(self, bastion: _Key) -> None:
        now = int(time.time())
        ok = _sign(bastion, _bastion_claims(iat=now, exp=now + 300))
        too_long = _sign(bastion, _bastion_claims(iat=now, exp=now + 301))
        assert _get(_app(), ok)[0] == 200
        assert _get(_app(), too_long) == INVALID

    @pytest.mark.parametrize("delta", [0, -1, -30])
    def test_exp_not_after_iat_is_invalid(self, bastion: _Key, delta: int) -> None:
        now = int(time.time())
        assert _get(_app(), _sign(bastion, _bastion_claims(iat=now, exp=now + delta))) == INVALID

    @pytest.mark.parametrize(
        "overrides",
        [
            {"exp": _DROP},
            {"iat": _DROP},
            {"exp": "9999999999"},
            {"iat": 1.5},
            {"exp": True},
            {"iss": _DROP},
            {"iss": "someone-else"},
            {"sub": _DROP},
            {"sub": ""},
            {"sub": 7},
        ],
        ids=lambda o: ",".join(f"{k}={v!r}" for k, v in o.items()),
    )
    def test_malformed_claims_are_invalid(
        self, bastion: _Key, overrides: dict[str, object]
    ) -> None:
        assert _get(_app(), _sign(bastion, _bastion_claims(**overrides))) == INVALID

    def test_garbage_token_is_invalid(self) -> None:
        assert _get(_app(), "not-a-jwt") == INVALID
        assert _get(_app(), "a.b.c") == INVALID

    def test_unknown_key_is_invalid(self) -> None:
        stranger = _keypair()
        assert _get(_app(), _sign(stranger, _bastion_claims())) == INVALID


class TestTokenTransport:
    def test_bearer_token_is_read_when_header_absent(self, bastion: _Key) -> None:
        token = _sign(bastion, _bastion_claims())
        resp = _app().get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_platform_header_wins_over_bearer(self, bastion: _Key) -> None:
        good = _sign(bastion, _bastion_claims(sub="v_header0001"))
        other = _sign(bastion, _bastion_claims(sub="v_bearer0001"))
        resp = _app().get(
            "/protected",
            headers={"X-Platform-Token": good, "Authorization": f"Bearer {other}"},
        )
        assert resp.json()["sub"] == "v_header0001"

    def test_non_bearer_authorization_is_no_token(self) -> None:
        resp = _app().get("/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert (resp.status_code, resp.json()) == MISSING

    def test_rejection_warning_names_the_check_and_never_the_token(
        self, bastion: _Key, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=platform_token.__name__)
        token = _sign(bastion, _bastion_claims(aud="paper-trail"))
        assert _get(_app(), token) == INVALID
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("aud" in m for m in warnings), warnings
        assert token not in caplog.text
        assert token.split(".")[1] not in caplog.text


class TestSettings:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, False),
            ("", False),
            ("false", False),
            ("0", False),
            ("no", False),
            ("maybe", False),
            ("true", True),
            (" TRUE ", True),
            ("1", True),
            ("yes", True),
            ("on", True),
        ],
    )
    def test_demo_mode_defaults_false_and_parses_strictly(
        self, raw: str | None, expected: bool
    ) -> None:
        environ = {} if raw is None else {"DEMO_MODE": raw}
        assert PlatformAuthSettings.from_env(environ).demo_mode is expected

    @pytest.mark.parametrize(
        ("app_env", "expected"),
        [(None, False), ("production", False), ("development", False), ("test", True)],
    )
    def test_test_settings_only_under_app_env_test(
        self, app_env: str | None, expected: bool
    ) -> None:
        environ = {} if app_env is None else {"APP_ENV": app_env}
        assert PlatformAuthSettings.from_env(environ).test_settings is expected

    def test_test_settings_with_no_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(BASTION_ENV)
        monkeypatch.delenv(FRONTEND_ENV)
        assert _get(_app(test_settings=True))[0] == 200

    def test_test_settings_with_a_key_still_enforce(self) -> None:
        assert _get(_app(test_settings=True)) == MISSING


class TestPublicPaths:
    @pytest.mark.parametrize(
        "path",
        [
            "/health",
            "/version",
            "/metrics",
            "/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
        ],
    )
    def test_public(self, path: str) -> None:
        assert platform_token.is_public_path(path)

    @pytest.mark.parametrize(
        "path", ["/docsx", "/redocs", "/healthz", "/sources", "/api/scrape/x/once", "/"]
    )
    def test_not_public(self, path: str) -> None:
        assert not platform_token.is_public_path(path)


class TestMalformedKey:
    def test_malformed_key_warns_once_by_name_and_counts_as_absent(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = "not base64 at all!!"
        monkeypatch.setenv(BASTION_ENV, bad)
        monkeypatch.delenv(FRONTEND_ENV)
        caplog.set_level(logging.WARNING, logger=platform_token.__name__)
        client = _app()
        assert _get(client) == (503, {"detail": "platform_auth_unconfigured"})
        assert _get(client) == (503, {"detail": "platform_auth_unconfigured"})
        named = [r for r in caplog.records if BASTION_ENV in r.getMessage()]
        assert len(named) == 1
        assert bad not in caplog.text

    def test_key_with_bom_and_crlf_is_parsed(
        self, monkeypatch: pytest.MonkeyPatch, bastion: _Key
    ) -> None:
        monkeypatch.setenv(BASTION_ENV, f"﻿{bastion.pub_b64}\r\n")
        assert _get(_app(), _sign(bastion, _bastion_claims()))[0] == 200


# ── Composed app: CORS outermost, limiter keyed on the principal ─────────────

ORIGIN = "https://magpie-frontend-three.vercel.app"


class TestComposedApp:
    def test_preflight_for_a_protected_route_allows_the_platform_header(self) -> None:
        client = TestClient(create_app(auth=PlatformAuthSettings(False, False)))
        resp = client.options(
            "/sources",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-platform-token, x-request-id, content-type",
            },
        )
        assert resp.status_code == 200
        allowed = resp.headers["access-control-allow-headers"].lower()
        for header in ("x-platform-token", "x-request-id", "content-type"):
            assert header in allowed
        assert resp.headers["access-control-allow-origin"] == ORIGIN

    def test_a_401_carries_cors_headers_and_a_request_id(self) -> None:
        client = TestClient(create_app(auth=PlatformAuthSettings(False, False)))
        resp = client.get("/sources", headers={"Origin": ORIGIN})
        assert resp.status_code == 401
        assert resp.headers["access-control-allow-origin"] == ORIGIN
        assert resp.headers["x-request-id"]

    def test_authenticated_visitors_get_buckets_by_iss_and_sub(self, bastion: _Key) -> None:
        app = create_app(auth=PlatformAuthSettings(False, False), rate_limit_per_minute=1)
        client = TestClient(ProxyHeadersMiddleware(app, trusted_hosts="*"))
        same_ip = {"X-Forwarded-For": "203.0.113.7"}

        def post(sub: str | None, headers: dict[str, str]) -> int:
            h = dict(headers)
            if sub is not None:
                h["X-Platform-Token"] = _sign(bastion, _bastion_claims(sub=sub))
            # A route that does not exist still passes both middlewares first.
            return client.post("/__rate_probe__", headers=h).status_code

        assert post("v_alice00001", same_ip) == 404
        assert post("v_alice00001", same_ip) == 429
        # Another visitor from the same address has their own bucket.
        assert post("v_bob0000001", same_ip) == 404

    def test_unauthenticated_demo_callers_fall_back_to_the_forwarded_address(self) -> None:
        app = create_app(auth=PlatformAuthSettings(True, False), rate_limit_per_minute=1)
        client = TestClient(ProxyHeadersMiddleware(app, trusted_hosts="*"))
        first = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
        second = {"X-Forwarded-For": "198.51.100.9, 10.0.0.1"}
        assert client.post("/__rate_probe__", headers=first).status_code == 404
        assert client.post("/__rate_probe__", headers=first).status_code == 429
        assert client.post("/__rate_probe__", headers=second).status_code == 404


class TestPrincipal:
    def test_rate_limit_key(self) -> None:
        assert Principal(iss="bastion", sub="v_x", role="viewer").rate_limit_key == "bastion:v_x"
