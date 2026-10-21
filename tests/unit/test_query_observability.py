"""slowquery-detective wiring: who may read /_slowquery, and where it stores (spec 11)."""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.requests import Request

from magpie.main import create_app
from magpie.platform import platform_token
from magpie.platform.platform_token import PlatformAuthSettings
from magpie.platform.principal import Minter, Principal, attach_principal
from magpie.platform.query_observability import authorize_bastion, store_url_for

QUERIES = "/_slowquery/queries?request_id=R-1"


def _keypair() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    der = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return pem, base64.b64encode(der).decode()


def _token(priv_pem: str, *, iss: str, sub: str, role: str) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": "magpie",
        "service": "magpie",
        "sub": sub,
        "role": role,
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, priv_pem, algorithm="EdDSA")


@asynccontextmanager
async def _no_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Skip magpie's own startup (file-source sync, Procrastinate) in these tests."""
    yield


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    bastion_priv, bastion_pub = _keypair()
    frontend_priv, frontend_pub = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", bastion_pub)
    monkeypatch.setenv("MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC", frontend_pub)
    monkeypatch.delenv("BASTION_PUBLIC_KEY_URL", raising=False)
    platform_token.reset_public_key_cache()
    yield bastion_priv, frontend_priv
    platform_token.reset_public_key_cache()


def _client(tmp_path, *, demo_mode: bool = False, test_settings: bool = False) -> TestClient:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sq.db'}")
    app = create_app(
        auth=PlatformAuthSettings(demo_mode=demo_mode, test_settings=test_settings),
        engine=engine,
        database_url=None,
        lifespan=_no_lifespan,
    )
    return TestClient(app)


class TestStoreUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "postgres://u:p@host/db",
            "postgresql://u:p@host/db?sslmode=require",
            "postgresql+asyncpg://u:p@host/db",
            "postgresql+psycopg://u:p@host/db",
        ],
    )
    def test_postgres_urls_are_the_store(self, url: str) -> None:
        assert store_url_for(url) == url

    @pytest.mark.parametrize(
        "url", [None, "", "   ", "sqlite+aiosqlite:///magpie.db", "mysql://u@h/db"]
    )
    def test_anything_else_means_the_in_memory_store(self, url: str | None) -> None:
        assert store_url_for(url) is None

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert store_url_for("  postgresql://u@h/db\n") == "postgresql://u@h/db"


def _request_with(principal: Principal | None) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    if principal is not None:
        attach_principal(request, principal)
    return request


class TestAuthorizeBastion:
    async def test_bastion_principal_is_allowed(self) -> None:
        bastion = Principal(iss="bastion", sub="v_x", role="viewer", minter=Minter.bastion)
        assert await authorize_bastion(_request_with(bastion)) is True

    async def test_frontend_principal_is_refused(self) -> None:
        frontend = Principal(
            iss="magpie-frontend", sub="magpie-frontend", role="frontend", minter=Minter.frontend
        )
        assert await authorize_bastion(_request_with(frontend)) is False

    async def test_anonymous_is_refused(self) -> None:
        assert await authorize_bastion(_request_with(None)) is False


class TestSlowqueryRoutesOnTheApp:
    def test_bastion_token_reads_the_queries(self, tmp_path, keys) -> None:
        bastion_priv, _ = keys
        token = _token(bastion_priv, iss="bastion", sub="v_0123456789", role="viewer")
        with _client(tmp_path) as client:
            resp = client.get(QUERIES, headers={"X-Platform-Token": token})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_no_token_is_401(self, tmp_path, keys) -> None:
        with _client(tmp_path) as client:
            resp = client.get(QUERIES)
        assert (resp.status_code, resp.json()) == (401, {"detail": "missing_platform_token"})

    def test_invalid_token_is_401(self, tmp_path, keys) -> None:
        bastion_priv, _ = keys
        wrong_aud = _token(bastion_priv, iss="bastion", sub="v_x", role="viewer").replace(
            ".", ".x", 1
        )
        with _client(tmp_path) as client:
            resp = client.get(QUERIES, headers={"X-Platform-Token": wrong_aud})
        assert (resp.status_code, resp.json()) == (401, {"detail": "invalid_platform_token"})

    def test_frontend_token_is_403(self, tmp_path, keys) -> None:
        _, frontend_priv = keys
        token = _token(frontend_priv, iss="magpie-frontend", sub="magpie-frontend", role="frontend")
        with _client(tmp_path) as client:
            resp = client.get(QUERIES, headers={"X-Platform-Token": token})
        assert resp.status_code == 403

    def test_demo_mode_without_a_token_is_403(self, tmp_path, keys) -> None:
        with _client(tmp_path, demo_mode=True) as client:
            assert client.get(QUERIES).status_code == 403

    def test_test_settings_without_keys_is_403(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("BASTION_SIGNING_KEY_PUBLIC", raising=False)
        monkeypatch.delenv("MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC", raising=False)
        platform_token.reset_public_key_cache()
        with _client(tmp_path, test_settings=True) as client:
            assert client.get(QUERIES).status_code == 403

    def test_routes_stay_out_of_the_openapi_document(self, tmp_path, keys) -> None:
        with _client(tmp_path) as client:
            paths = client.get("/openapi.json").json()["paths"]
        assert not [p for p in paths if p.startswith("/_slowquery")]
