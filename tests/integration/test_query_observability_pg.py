"""WP-8 acceptance: a request's queries are listed by request id, on real Postgres (spec 11).

A scrape tagged ``X-Request-Id: R`` runs against a Testcontainers Postgres through
the real app (contract-v2 auth, slowquery-detective's Postgres store in the same
database). ``GET /_slowquery/queries?request_id=R`` then lists the statements that
scrape caused — for bastion only.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from magpie.api.deps import get_db_session, get_session_factory_dep
from magpie.config.schema import SourceConfig
from magpie.main import create_app
from magpie.platform import platform_token
from magpie.platform.platform_token import PlatformAuthSettings
from magpie.storage.models import Base, SourceOrigin
from magpie.storage.sources_repo import SourcesRepository

from .conftest import PgUrls

pytestmark = pytest.mark.slow

SOURCE = "wp8-src"
SOURCE_YAML = f"""\
name: {SOURCE}
url: https://example.com/list
schedule: "0 */6 * * *"
item:
  container: "div.card"
  fields:
    - {{ name: title, selector: "h2::text" }}
    - {{ name: id, selector: "::attr(data-id)" }}
  dedupe_key: id
health:
  min_items: 0
"""
RAW_ITEMS = [
    {"id": "a1", "title": "First", "url": "https://example.com/a1"},
    {"id": "a2", "title": "Second", "url": "https://example.com/a2"},
]
R = "R-wp8-scrape-1"


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
    """magpie's own startup (file sync, Procrastinate) is not under test here."""
    yield


async def _seed(async_url: str) -> None:
    engine = create_async_engine(async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        cfg = SourceConfig(**yaml.safe_load(SOURCE_YAML))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await SourcesRepository(session).create(
                config=cfg, origin=SourceOrigin.api, yaml_text=SOURCE_YAML
            )
            await session.commit()
    finally:
        await engine.dispose()


def test_a_tagged_scrape_is_listed_by_request_id_for_bastion_only(
    pg_database: PgUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_seed(pg_database.async_url))

    bastion_priv, bastion_pub = _keypair()
    frontend_priv, frontend_pub = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", bastion_pub)
    monkeypatch.setenv("MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC", frontend_pub)
    monkeypatch.delenv("BASTION_PUBLIC_KEY_URL", raising=False)
    platform_token.reset_public_key_cache()
    bastion = {
        "X-Platform-Token": _token(bastion_priv, iss="bastion", sub="v_0123456789", role="viewer")
    }
    frontend = {
        "X-Platform-Token": _token(
            frontend_priv, iss="magpie-frontend", sub="magpie-frontend", role="frontend"
        )
    }

    # The app's engine is the one the store observes; NullPool keeps connections
    # off the seeding loop. The stock postgresql:// URL is what Render hands out.
    engine = create_async_engine(pg_database.async_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app = create_app(
        auth=PlatformAuthSettings(demo_mode=False, test_settings=False),
        rate_limit_per_minute=0,
        engine=engine,
        database_url=pg_database.plain_url,
        lifespan=_no_lifespan,
    )
    app.dependency_overrides[get_session_factory_dep] = lambda: factory
    app.dependency_overrides[get_db_session] = _session

    def queries(request_id: str, headers: dict[str, str]) -> tuple[int, object]:
        resp = client.get(f"/_slowquery/queries?request_id={request_id}", headers=headers)
        return resp.status_code, resp.json()

    with (
        patch(
            "magpie.services.scrape_service._execute",
            new=AsyncMock(return_value=RAW_ITEMS),
        ),
        TestClient(app) as client,
    ):
        tagged = client.post(f"/api/scrape/{SOURCE}/once", headers={**bastion, "X-Request-Id": R})
        assert tagged.status_code == 200, tagged.text
        assert len(tagged.json()["items"]) == 2

        status, rows = queries(R, {**bastion, "X-Request-Id": R})
        assert status == 200
        assert isinstance(rows, list) and rows, "the tagged scrape's statements must be listed"
        sql = " ".join(row["fingerprint"].lower() for row in rows)
        assert "runs" in sql and "items" in sql, sql
        for row in rows:
            assert row["call_count"] >= 1
            assert row["p50_ms"] is None and row["p95_ms"] is None and row["p99_ms"] is None
        counts = {row["id"]: row["call_count"] for row in rows}

        # An untagged scrape, and the measuring call itself, add nothing to R.
        untagged = client.post(f"/api/scrape/{SOURCE}/once", headers=bastion)
        assert untagged.status_code == 200
        status, again = queries(R, {**bastion, "X-Request-Id": R})
        assert status == 200
        assert {row["id"]: row["call_count"] for row in again} == counts  # type: ignore[union-attr]

        assert queries("R-never-seen", bastion) == (200, [])
        assert queries(R, {}) == (401, {"detail": "missing_platform_token"})
        assert queries(R, frontend)[0] == 403

    # Persisted in magpie's own database, schema `slowquery`, under service "magpie".
    sync = create_engine(pg_database.sync_url, poolclass=NullPool)
    try:
        with sync.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT count(*) FROM slowquery.request_stats "
                    "WHERE service = 'magpie' AND request_id = :r"
                ),
                {"r": R},
            ).scalar_one()
    finally:
        sync.dispose()
    assert stored == len(counts)
    asyncio.run(engine.dispose())
