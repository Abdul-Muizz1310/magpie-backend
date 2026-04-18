"""SQLAlchemy 2.0 models for magpie persistence.

Four tables cover every entity the service layer owns. Uuid + JSON use
SQLAlchemy's generic types so SQLite (used in tests) and Postgres (used in
production) both work without forking the code.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class SourceOrigin(enum.StrEnum):
    """Where a source config came from.

    ``file``: committed YAML under ``configs/``, read-only via the API.
    ``api``: submitted at runtime via ``POST /api/sources``, mutable.
    """

    file = "file"
    api = "api"


class RunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    ok = "ok"
    error = "error"


class HealMode(enum.StrEnum):
    """How a heal was applied.

    ``pr``: GitHub PR opened against the committed YAML (file origin).
    ``db_patch``: ``sources.config_yaml`` updated in place (api origin).
    """

    pr = "pr"
    db_patch = "db_patch"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    origin: Mapped[SourceOrigin] = mapped_column(
        Enum(SourceOrigin, name="source_origin"),
        default=SourceOrigin.api,
        nullable=False,
    )
    config_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    config_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    runs: Mapped[list[Run]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    items: Mapped[list[Item]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    heals: Mapped[list[Heal]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.queued, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_removed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="runs")
    heals: Mapped[list[Heal]] = relationship(back_populates="run")


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("source_id", "dedupe_key", name="uq_items_source_dedupe"),
        Index("ix_items_source_removed", "source_id", "removed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    removed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="items")


class Heal(Base):
    __tablename__ = "heals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True, nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    old_selector: Mapped[str] = mapped_column(Text, nullable=False)
    new_selector: Mapped[str] = mapped_column(Text, nullable=False)
    selector_type: Mapped[str] = mapped_column(String(16), default="css", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    sample_values: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    mode: Mapped[HealMode] = mapped_column(Enum(HealMode, name="heal_mode"), nullable=False)
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="heals")
    run: Mapped[Run | None] = relationship(back_populates="heals")


__all__ = [
    "Base",
    "Heal",
    "HealMode",
    "Item",
    "Run",
    "RunStatus",
    "Source",
    "SourceOrigin",
]
