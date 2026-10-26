"""Shared fixtures for magpie tests.

``db_session`` provides a per-test AsyncSession bound to an ephemeral SQLite
file whose schema is built from ``Base.metadata`` (no Alembic round-trip per
test). Tests that want the full Alembic upgrade path should use the dedicated
fixtures in ``tests/unit/test_models_and_migrations.py``.
"""

from __future__ import annotations

import os

# Disable the per-IP rate limiter for the shared ``magpie.main.app`` used across
# router tests (the limit is read once at app-import time). The limiter's own
# behaviour is covered in tests/unit/test_rate_limit.py with an explicit limit.
# Must run before any test module imports ``magpie.main``.
os.environ["RATE_LIMIT_PER_MINUTE"] = "0"
# The contract-v2 "test settings" (spec 10): with no trusted key configured, the
# shared app lets requests through instead of answering 503, so router tests need
# no tokens. Tests that need production behaviour build their own app with
# ``PlatformAuthSettings(test_settings=False)``.
os.environ["APP_ENV"] = "test"
# Tracing stays off whatever the shell exports (docs/specs/12-tracing.md): blank
# Langfuse keys, so no test sends spans to Langfuse. Tests that assert on traces
# take the ``traces`` fixture, a client on an in-memory exporter.
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from magpie.core import tracing
from magpie.storage.models import Base

_METADATA = "langfuse.observation.metadata."


@dataclass
class Traces:
    """The spans a test exported, and readers for the Langfuse attributes on them."""

    client: Langfuse
    exporter: InMemorySpanExporter

    def spans(self) -> list[ReadableSpan]:
        self.client.flush()
        return list(self.exporter.get_finished_spans())

    def named(self, name: str) -> list[ReadableSpan]:
        return sorted((s for s in self.spans() if s.name == name), key=lambda s: s.start_time or 0)

    def one(self, name: str) -> ReadableSpan:
        found = self.named(name)
        assert len(found) == 1, f"{len(found)} spans named {name!r}"
        return found[0]

    def parent(self, span: ReadableSpan) -> ReadableSpan | None:
        if span.parent is None:
            return None
        return next((s for s in self.spans() if s.context.span_id == span.parent.span_id), None)

    def children(self, span: ReadableSpan) -> list[ReadableSpan]:
        kids = [
            s
            for s in self.spans()
            if s.parent is not None and s.parent.span_id == span.context.span_id
        ]
        return sorted(kids, key=lambda s: s.start_time or 0)

    @staticmethod
    def attr(span: ReadableSpan, key: str) -> Any:
        return (span.attributes or {}).get(key)

    @staticmethod
    def decoded(span: ReadableSpan, key: str) -> Any:
        raw = (span.attributes or {}).get(key)
        return None if raw is None else json.loads(str(raw))

    @staticmethod
    def type_of(span: ReadableSpan) -> Any:
        return (span.attributes or {}).get("langfuse.observation.type")

    @staticmethod
    def level(span: ReadableSpan) -> str:
        """The level Langfuse derives: the explicit attribute, else ERROR on an error status."""
        explicit = (span.attributes or {}).get("langfuse.observation.level")
        if explicit is not None:
            return str(explicit)
        return "ERROR" if span.status.status_code is StatusCode.ERROR else "DEFAULT"

    @staticmethod
    def status_message(span: ReadableSpan) -> str | None:
        explicit = (span.attributes or {}).get("langfuse.observation.status_message")
        return str(explicit) if explicit is not None else span.status.description

    @staticmethod
    def metadata(span: ReadableSpan) -> dict[str, Any]:
        return {
            key.removeprefix(_METADATA): json.loads(str(value))
            for key, value in (span.attributes or {}).items()
            if key.startswith(_METADATA)
        }


def tracing_client(exporter: SpanExporter) -> Langfuse:
    """A Langfuse client that exports to ``exporter`` only, never to the network.

    A fresh public key per client: the SDK keeps one set of resources per key.
    """
    return Langfuse(
        public_key=f"pk-lf-test-{uuid4().hex}",
        secret_key="sk-lf-test",
        base_url="http://langfuse.invalid",
        tracer_provider=TracerProvider(),
        span_exporter=exporter,
        environment="test",
    )


@pytest.fixture
def traces(monkeypatch: pytest.MonkeyPatch) -> Iterator[Traces]:
    exporter = InMemorySpanExporter()
    client = tracing_client(exporter)
    monkeypatch.setattr(tracing, "_client", client)
    yield Traces(client=client, exporter=exporter)
    client.shutdown()


@pytest.fixture
async def db_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    db_file = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def db_session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
