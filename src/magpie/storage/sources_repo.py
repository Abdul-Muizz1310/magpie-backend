"""CRUD for Source rows.

The service layer uses this repo for every source lookup; the router never
touches the ORM directly. All methods are async and expect an ``AsyncSession``
handed in at construction time so the session's transaction boundary stays in
the caller's hands (FastAPI ``Depends`` or a queue task).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from magpie.config.schema import SourceConfig
from magpie.storage.models import Item, Run, RunStatus, Source, SourceOrigin


@dataclass(frozen=True)
class SourceStats:
    """Per-source aggregates used by the ``/sources`` viewer endpoint."""

    source: Source
    item_count: int
    last_run_at: datetime | None
    last_status: RunStatus | None


def _compute_sha(yaml_text: str) -> str:
    return hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()[:12]


class DuplicateSourceError(Exception):
    """Raised when attempting to insert a Source whose name is already taken."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Source {name!r} already exists")
        self.name = name


class SourceNotFoundError(Exception):
    """Raised when a source name does not exist."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Source {name!r} not found")
        self.name = name


class ImmutableSourceError(Exception):
    """Raised when attempting to mutate a file-origin source via the API."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Source {name!r} is file-origin and cannot be modified at runtime")
        self.name = name


class SourcesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Reads ────────────────────────────────────────────────────────────

    async def get_by_name(self, name: str) -> Source | None:
        result = await self._session.execute(select(Source).where(Source.name == name))
        return result.scalar_one_or_none()

    async def list_all(self, *, origin: SourceOrigin | None = None) -> Sequence[Source]:
        stmt = select(Source).order_by(Source.name)
        if origin is not None:
            stmt = stmt.where(Source.origin == origin)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_with_stats(self, *, origin: SourceOrigin | None = None) -> Sequence[SourceStats]:
        """Return every source with its item_count + latest-run info.

        Replaces the previous O(N) round-trips from the ``/sources`` view.
        Three queries total, regardless of how many sources exist:
          1. the Source rows themselves;
          2. non-removed item counts via ``GROUP BY source_id``;
          3. the most-recent ``runs`` row per source via a ROW_NUMBER()
             window function.
        """
        sources = list(await self.list_all(origin=origin))
        if not sources:
            return []

        source_ids = [s.id for s in sources]

        count_stmt = (
            select(Item.source_id, func.count(Item.id).label("n"))
            .where(Item.source_id.in_(source_ids))
            .where(Item.removed.is_(False))
            .group_by(Item.source_id)
        )
        counts: dict[uuid.UUID, int] = {
            row.source_id: int(row.n) for row in (await self._session.execute(count_stmt)).all()
        }

        rn = (
            func.row_number()
            .over(
                partition_by=Run.source_id,
                order_by=Run.started_at.desc(),
            )
            .label("rn")
        )
        ranked = (
            select(Run.source_id, Run.started_at, Run.status, rn)
            .where(Run.source_id.in_(source_ids))
            .subquery()
        )
        latest_stmt = select(ranked.c.source_id, ranked.c.started_at, ranked.c.status).where(
            ranked.c.rn == 1
        )
        latest: dict[uuid.UUID, tuple[datetime, RunStatus]] = {
            row.source_id: (row.started_at, row.status)
            for row in (await self._session.execute(latest_stmt)).all()
        }

        return [
            SourceStats(
                source=src,
                item_count=counts.get(src.id, 0),
                last_run_at=latest[src.id][0] if src.id in latest else None,
                last_status=latest[src.id][1] if src.id in latest else None,
            )
            for src in sources
        ]

    async def get_config(self, name: str) -> SourceConfig:
        source = await self.get_by_name(name)
        if source is None:
            raise SourceNotFoundError(name)
        data = yaml.safe_load(source.config_yaml)
        return SourceConfig(**data)

    # ── Writes ───────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        config: SourceConfig,
        origin: SourceOrigin,
        yaml_text: str | None = None,
    ) -> Source:
        existing = await self.get_by_name(config.name)
        if existing is not None:
            raise DuplicateSourceError(config.name)

        text = (
            yaml_text
            if yaml_text is not None
            else yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
        )
        source = Source(
            name=config.name,
            description=config.description,
            origin=origin,
            config_yaml=text,
            config_sha=_compute_sha(text),
        )
        self._session.add(source)
        await self._session.flush()
        return source

    async def update_config(
        self,
        *,
        name: str,
        config: SourceConfig,
        yaml_text: str | None = None,
        allow_file_origin: bool = False,
    ) -> Source:
        source = await self.get_by_name(name)
        if source is None:
            raise SourceNotFoundError(name)
        if source.origin is SourceOrigin.file and not allow_file_origin:
            raise ImmutableSourceError(name)

        text = (
            yaml_text
            if yaml_text is not None
            else yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
        )
        source.description = config.description
        source.config_yaml = text
        source.config_sha = _compute_sha(text)
        await self._session.flush()
        return source

    async def upsert_file_source(self, *, config: SourceConfig, yaml_text: str) -> Source:
        """Idempotent insert/update of a file-origin source.

        Used by the FastAPI lifespan to mirror ``configs/*.yaml`` into the DB
        on startup. Safe to call repeatedly — re-running with the same YAML
        produces no writes beyond the ``updated_at`` refresh.
        """
        source = await self.get_by_name(config.name)
        if source is None:
            source = Source(
                name=config.name,
                description=config.description,
                origin=SourceOrigin.file,
                config_yaml=yaml_text,
                config_sha=_compute_sha(yaml_text),
            )
            self._session.add(source)
            await self._session.flush()
            return source

        # Existing — only patch if content actually changed.
        new_sha = _compute_sha(yaml_text)
        if source.config_sha != new_sha:
            source.description = config.description
            source.config_yaml = yaml_text
            source.config_sha = new_sha
            source.origin = SourceOrigin.file
            await self._session.flush()
        return source

    async def delete(self, *, name: str, allow_file_origin: bool = False) -> None:
        source = await self.get_by_name(name)
        if source is None:
            raise SourceNotFoundError(name)
        if source.origin is SourceOrigin.file and not allow_file_origin:
            raise ImmutableSourceError(name)
        await self._session.delete(source)
        await self._session.flush()
