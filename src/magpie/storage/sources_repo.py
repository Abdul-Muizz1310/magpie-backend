"""CRUD for Source rows.

The service layer uses this repo for every source lookup; the router never
touches the ORM directly. All methods are async and expect an ``AsyncSession``
handed in at construction time so the session's transaction boundary stays in
the caller's hands (FastAPI ``Depends`` or a queue task).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from magpie.config.schema import SourceConfig
from magpie.storage.models import Source, SourceOrigin


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
