"""Tests for the X-Platform-Token middleware (Ed25519 platform JWT verification)."""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magpie.platform import platform_token
from magpie.platform.platform_token import install_platform_token


def _make_app(*, demo_mode: bool) -> TestClient:
    app = FastAPI()
    install_platform_token(app, demo_mode=demo_mode)

    @app.get("/health", include_in_schema=False)
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/protected")
    async def _protected() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def _keypair() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_der = priv.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, base64.b64encode(pub_der).decode()


def _token(priv_pem: str, *, expired: bool = False) -> str:
    now = dt.datetime.now(dt.UTC)
    exp = now - dt.timedelta(seconds=10) if expired else now + dt.timedelta(seconds=60)
    return jwt.encode(
        {"sub": "bastion", "role": "admin", "service": "magpie", "exp": exp},
        priv_pem,
        algorithm="EdDSA",
    )


_FRONTEND_ENV = "MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC"


def _frontend_token(
    priv_pem: str,
    *,
    service: str = "magpie",
    role: str = "frontend",
    exp_offset_s: int | None = 60,
) -> str:
    """A token shaped like magpie-frontend's (DR-0030), signed with ``priv_pem``."""
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": "magpie-frontend",
        "role": role,
        "service": service,
        "iat": now,
        "jti": str(uuid.uuid4()),
    }
    if exp_offset_s is not None:
        claims["exp"] = now + exp_offset_s
    return jwt.encode(
        claims, priv_pem, algorithm="EdDSA", headers={"kid": "magpie-frontend-2026-10"}
    )


def _b64url(data: dict[str, object]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASTION_SIGNING_KEY_PUBLIC", raising=False)
    monkeypatch.delenv("BASTION_PUBLIC_KEY_URL", raising=False)
    monkeypatch.delenv(_FRONTEND_ENV, raising=False)
    platform_token.reset_public_key_cache()


def test_demo_mode_accepts_without_token() -> None:
    client = _make_app(demo_mode=True)
    assert client.get("/protected").status_code == 200


def test_non_demo_without_key_fails_open() -> None:
    client = _make_app(demo_mode=False)
    assert client.get("/protected").status_code == 200


def test_non_demo_with_key_rejects_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    client = _make_app(demo_mode=False)
    assert client.get("/protected").status_code == 401
    # Platform endpoints stay exempt even when enforcement is on.
    assert client.get("/health").status_code == 200


def test_non_demo_with_key_accepts_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    client = _make_app(demo_mode=False)
    headers = {"X-Platform-Token": _token(priv_pem)}
    assert client.get("/protected", headers=headers).status_code == 200


def test_non_demo_with_key_rejects_tampered_token(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    client = _make_app(demo_mode=False)
    headers = {"X-Platform-Token": f"{_token(priv_pem)}tampered"}
    assert client.get("/protected", headers=headers).status_code == 401


def test_non_demo_with_key_rejects_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    client = _make_app(demo_mode=False)
    headers = {"X-Platform-Token": _token(priv_pem, expired=True)}
    assert client.get("/protected", headers=headers).status_code == 401


def test_public_key_fetched_from_url_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_PUBLIC_KEY_URL", "https://bastion.example/api/public-key")

    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"kid": "test", "algorithm": "EdDSA", "publicKey": pub_b64}

    class _FakeAsyncClient:
        async def get(self, url: str) -> _Resp:
            calls["n"] += 1
            return _Resp()

    # The key fetch must be async (P10): patch the shared async client.
    monkeypatch.setattr(platform_token, "_get_http_client", lambda: _FakeAsyncClient())
    client = _make_app(demo_mode=False)
    headers = {"X-Platform-Token": _token(priv_pem)}
    assert client.get("/protected", headers=headers).status_code == 200
    # Second request reuses the cached key (no second fetch).
    assert client.get("/protected", headers=headers).status_code == 200
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_load_public_key_pem_is_async(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_public_key_pem returns a coroutine and resolves the env DER key."""
    _, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    pem = await platform_token.load_public_key_pem()
    assert pem is not None
    assert pem.startswith("-----BEGIN PUBLIC KEY-----")


# ─── Frontend signing key (docs/specs/09-platform-token.md) ───────────────────


def _protected(client: TestClient, token: str | None = None) -> int:
    headers = {"X-Platform-Token": token} if token is not None else {}
    return client.get("/protected", headers=headers).status_code


def _both_keys(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Configure a bastion key and a frontend key; return both private PEMs."""
    bastion_priv, bastion_pub = _keypair()
    frontend_priv, frontend_pub = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", bastion_pub)
    monkeypatch.setenv(_FRONTEND_ENV, frontend_pub)
    return bastion_priv, frontend_priv


def test_frontend_token_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _, frontend_priv = _both_keys(monkeypatch)
    client = _make_app(demo_mode=False)
    assert _protected(client, _frontend_token(frontend_priv)) == 200


def test_bastion_token_still_accepted_with_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    bastion_priv, _ = _both_keys(monkeypatch)
    client = _make_app(demo_mode=False)
    assert _protected(client, _token(bastion_priv)) == 200


def test_expired_frontend_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _, frontend_priv = _both_keys(monkeypatch)
    client = _make_app(demo_mode=False)
    assert _protected(client, _frontend_token(frontend_priv, exp_offset_s=-10)) == 401


def test_token_from_unknown_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _both_keys(monkeypatch)
    stranger_priv, _ = _keypair()
    client = _make_app(demo_mode=False)
    assert _protected(client, _frontend_token(stranger_priv)) == 401
    assert _protected(client, _token(stranger_priv)) == 401


def test_frontend_token_for_another_service_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _, frontend_priv = _both_keys(monkeypatch)
    client = _make_app(demo_mode=False)
    assert _protected(client, _frontend_token(frontend_priv, service="paper-trail")) == 401


def test_frontend_token_with_other_role_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _, frontend_priv = _both_keys(monkeypatch)
    client = _make_app(demo_mode=False)
    assert _protected(client, _frontend_token(frontend_priv, role="admin")) == 401


def test_frontend_token_without_exp_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _, frontend_priv = _both_keys(monkeypatch)
    client = _make_app(demo_mode=False)
    assert _protected(client, _frontend_token(frontend_priv, exp_offset_s=None)) == 401


def test_bastion_token_without_exp_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    bastion_priv, _ = _both_keys(monkeypatch)
    client = _make_app(demo_mode=False)
    no_exp = jwt.encode(
        {"sub": "bastion", "role": "admin", "service": "magpie"}, bastion_priv, algorithm="EdDSA"
    )
    assert _protected(client, no_exp) == 401


def test_unsigned_token_with_frontend_claims_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _both_keys(monkeypatch)
    client = _make_app(demo_mode=False)
    claims: dict[str, object] = {
        "sub": "magpie-frontend",
        "role": "frontend",
        "service": "magpie",
        "exp": int(time.time()) + 60,
    }
    unsigned = f"{_b64url({'alg': 'none', 'typ': 'JWT'})}.{_b64url(claims)}."
    assert _protected(client, unsigned) == 401


def test_only_frontend_key_configured_enforces(monkeypatch: pytest.MonkeyPatch) -> None:
    frontend_priv, frontend_pub = _keypair()
    unconfigured_bastion_priv, _ = _keypair()
    monkeypatch.setenv(_FRONTEND_ENV, frontend_pub)
    client = _make_app(demo_mode=False)
    assert _protected(client, _frontend_token(frontend_priv)) == 200
    assert _protected(client) == 401
    assert _protected(client, _token(unconfigured_bastion_priv)) == 401


def test_frontend_key_with_bom_and_crlf_still_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    frontend_priv, frontend_pub = _keypair()
    monkeypatch.setenv(_FRONTEND_ENV, f"\ufeff{frontend_pub}\r\n")
    client = _make_app(demo_mode=False)
    assert _protected(client, _frontend_token(frontend_priv)) == 200
    assert _protected(client) == 401


def test_empty_frontend_key_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_FRONTEND_ENV, "  ")
    client = _make_app(demo_mode=False)
    assert _protected(client) == 200


def test_demo_mode_bypasses_with_frontend_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _both_keys(monkeypatch)
    client = _make_app(demo_mode=True)
    assert _protected(client) == 200


def test_exempt_path_needs_no_token_with_frontend_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _, frontend_pub = _keypair()
    monkeypatch.setenv(_FRONTEND_ENV, frontend_pub)
    client = _make_app(demo_mode=False)
    assert client.get("/health").status_code == 200
    assert _protected(client) == 401


def _p256_public_b64() -> str:
    der = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return base64.b64encode(der).decode()


_MALFORMED_KEYS = {
    "not-base64": "not base64 at all!!",
    "not-a-key": base64.b64encode(b"definitely not a DER public key").decode(),
    "p256-key": _p256_public_b64(),
}


@pytest.mark.parametrize("malformed", _MALFORMED_KEYS.values(), ids=_MALFORMED_KEYS.keys())
def test_malformed_frontend_key_warns_and_is_absent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, malformed: str
) -> None:
    bastion_priv, bastion_pub = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", bastion_pub)
    monkeypatch.setenv(_FRONTEND_ENV, malformed)
    # Migration tests run alembic in-process, whose env.py fileConfig() disables
    # every logger created before it; in a full run this one would be muted.
    monkeypatch.setattr(platform_token.logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger=platform_token.__name__)
    client = _make_app(demo_mode=False)

    assert _protected(client, _token(bastion_priv)) == 200
    assert _protected(client) == 401

    warnings = [r for r in caplog.records if _FRONTEND_ENV in r.getMessage()]
    assert len(warnings) == 1, "the malformed-key warning is logged once, not per request"
    assert warnings[0].levelno == logging.WARNING
    assert malformed not in caplog.text


def test_malformed_frontend_key_alone_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed key is *absent*, so with no bastion key the documented fail-open holds."""
    monkeypatch.setenv(_FRONTEND_ENV, "not base64 at all!!")
    client = _make_app(demo_mode=False)
    assert _protected(client) == 200


# ─── Clock skew between minter and verifier: 10 s leeway ──────────────────────


def _token_at(priv_pem: str, *, minter: str, iat_offset_s: int, exp_offset_s: int) -> str:
    """A bastion- or frontend-shaped token with ``iat``/``exp`` offset from now."""
    now = int(time.time())
    claims: dict[str, object] = {"iat": now + iat_offset_s, "exp": now + exp_offset_s}
    if minter == "frontend":
        claims |= {"sub": "magpie-frontend", "role": "frontend", "service": "magpie"}
    else:
        claims |= {"sub": "bastion", "role": "admin", "service": "magpie"}
    return jwt.encode(claims, priv_pem, algorithm="EdDSA")


# (iat offset, exp offset, expected status)
_SKEW_CASES = {
    "iat-5s-ahead": (5, 65, 200),
    "iat-30s-ahead": (30, 90, 401),
    "exp-5s-ago": (-65, -5, 200),
    "exp-30s-ago": (-90, -30, 401),
}


@pytest.mark.parametrize("minter", ["bastion", "frontend"])
@pytest.mark.parametrize(
    ("iat_offset_s", "exp_offset_s", "expected"), _SKEW_CASES.values(), ids=_SKEW_CASES.keys()
)
def test_clock_skew_within_leeway_tolerated(
    monkeypatch: pytest.MonkeyPatch,
    minter: str,
    iat_offset_s: int,
    exp_offset_s: int,
    expected: int,
) -> None:
    bastion_priv, frontend_priv = _both_keys(monkeypatch)
    priv = frontend_priv if minter == "frontend" else bastion_priv
    client = _make_app(demo_mode=False)
    token = _token_at(priv, minter=minter, iat_offset_s=iat_offset_s, exp_offset_s=exp_offset_s)
    assert _protected(client, token) == expected
