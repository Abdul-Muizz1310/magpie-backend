"""Trusted-key resolution for the platform token middleware (specs 09 and 10).

The verification rules themselves are the contract-v2 conformance tests in
``test_platform_contract_v2.py``; this file covers where the two trusted keys
come from: env values (BOM-stripped, malformed => absent, warned once) and
bastion's key fetched from ``BASTION_PUBLIC_KEY_URL`` and cached for an hour.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magpie.platform import platform_token
from magpie.platform.platform_token import PlatformAuthSettings, install_platform_token

_BASTION_ENV = "BASTION_SIGNING_KEY_PUBLIC"
_FRONTEND_ENV = "MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC"
_URL_ENV = "BASTION_PUBLIC_KEY_URL"


def _make_app() -> TestClient:
    app = FastAPI()
    install_platform_token(app, settings=PlatformAuthSettings(demo_mode=False, test_settings=False))

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


def _bastion_token(priv_pem: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "bastion",
            "aud": "magpie",
            "service": "magpie",
            "sub": "bastion",
            "role": "admin",
            "iat": now,
            "exp": now + 60,
            "jti": str(uuid.uuid4()),
        },
        priv_pem,
        algorithm="EdDSA",
    )


def _protected(client: TestClient, token: str | None = None) -> int:
    headers = {"X-Platform-Token": token} if token is not None else {}
    return client.get("/protected", headers=headers).status_code


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_BASTION_ENV, raising=False)
    monkeypatch.delenv(_URL_ENV, raising=False)
    monkeypatch.delenv(_FRONTEND_ENV, raising=False)
    # Migration tests run alembic in-process; keep this logger audible regardless.
    monkeypatch.setattr(platform_token.logger, "disabled", False)
    platform_token.reset_public_key_cache()


class _FakeKeyServer:
    def __init__(self, pub_b64: str) -> None:
        self.pub_b64 = pub_b64
        self.calls = 0

    async def get(self, url: str) -> _FakeKeyServer:
        self.calls += 1
        return self

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {"kid": "test", "algorithm": "EdDSA", "publicKey": self.pub_b64}


def test_bastion_key_fetched_from_url_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv(_URL_ENV, "https://bastion.example/api/public-key")
    server = _FakeKeyServer(pub_b64)
    # The key fetch must be async (P10): patch the shared async client.
    monkeypatch.setattr(platform_token, "_get_http_client", lambda: server)
    client = _make_app()
    assert _protected(client, _bastion_token(priv_pem)) == 200
    assert _protected(client, _bastion_token(priv_pem)) == 200
    assert server.calls == 1, "the second request reuses the cached key"


def test_env_key_wins_over_url(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    _, other_pub = _keypair()
    monkeypatch.setenv(_BASTION_ENV, pub_b64)
    monkeypatch.setenv(_URL_ENV, "https://bastion.example/api/public-key")
    server = _FakeKeyServer(other_pub)
    monkeypatch.setattr(platform_token, "_get_http_client", lambda: server)
    assert _protected(_make_app(), _bastion_token(priv_pem)) == 200
    assert server.calls == 0


def test_malformed_key_from_url_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_URL_ENV, "https://bastion.example/api/public-key")
    monkeypatch.setattr(platform_token, "_get_http_client", lambda: _FakeKeyServer("garbage!!"))
    assert _protected(_make_app()) == 503


async def test_load_bastion_key_is_async_and_parses_the_env_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, pub_b64 = _keypair()
    monkeypatch.setenv(_BASTION_ENV, pub_b64)
    key = await platform_token.load_bastion_key()
    assert isinstance(key, Ed25519PublicKey)


def test_empty_key_values_are_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_BASTION_ENV, "  ")
    monkeypatch.setenv(_FRONTEND_ENV, "﻿\r\n")
    assert _protected(_make_app()) == 503


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
def test_malformed_frontend_key_warns_once_and_is_absent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, malformed: str
) -> None:
    bastion_priv, bastion_pub = _keypair()
    monkeypatch.setenv(_BASTION_ENV, bastion_pub)
    monkeypatch.setenv(_FRONTEND_ENV, malformed)
    caplog.set_level(logging.WARNING, logger=platform_token.__name__)
    client = _make_app()

    assert _protected(client, _bastion_token(bastion_priv)) == 200
    assert _protected(client) == 401

    warnings = [r for r in caplog.records if _FRONTEND_ENV in r.getMessage()]
    assert len(warnings) == 1, "the malformed-key warning is logged once, not per request"
    assert warnings[0].levelno == logging.WARNING
    assert malformed not in caplog.text
