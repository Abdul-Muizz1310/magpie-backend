"""Custom-source CRUD endpoints — runtime-editable replacement for YAML files.

Only ``origin=api`` rows are mutable here; ``origin=file`` rows are surfaced
read-only so the committed ``configs/*.yaml`` stays authoritative. File-origin
rows must be edited in the repo and deployed.
"""

from __future__ import annotations

from typing import Annotated, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from magpie.api.deps import get_db_session
from magpie.config.schema import SourceConfig
from magpie.schemas.sources import SourceDetail, SourceSubmission, SourceSummary
from magpie.storage.models import Source, SourceOrigin
from magpie.storage.sources_repo import (
    DuplicateSourceError,
    ImmutableSourceError,
    SourceNotFoundError,
    SourcesRepository,
)

router = APIRouter(prefix="/api/sources", tags=["sources"])

_Session = Annotated[AsyncSession, Depends(get_db_session)]


def _parse_submission(body: SourceSubmission) -> tuple[SourceConfig, str]:
    """Turn a YAML/JSON body into a validated config + its canonical YAML text."""
    if not body.is_valid():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Body must include either 'yaml' or 'config'.",
        )

    if body.yaml is not None:
        try:
            data = yaml.safe_load(body.yaml)
        except yaml.YAMLError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid YAML: {exc}",
            ) from exc
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="YAML must parse to a mapping.",
            )
    else:
        data = body.config or {}

    try:
        config = SourceConfig(**data)
    except ValidationError as exc:
        # ``exc.errors()`` may include non-JSON-serialisable context (e.g.
        # nested exceptions from our selector validator), so stringify before
        # handing to FastAPI.
        serialisable = [
            {"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=serialisable,
        ) from exc

    canonical_yaml = (
        body.yaml
        if body.yaml is not None
        else yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    return config, canonical_yaml


def _summary(row: Source) -> SourceSummary:
    return SourceSummary(
        id=row.id,
        name=row.name,
        description=row.description,
        origin=row.origin.value,
        config_sha=row.config_sha,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _detail(row: Source) -> SourceDetail:
    return SourceDetail(
        id=row.id,
        name=row.name,
        description=row.description,
        origin=row.origin.value,
        config_sha=row.config_sha,
        created_at=row.created_at,
        updated_at=row.updated_at,
        config_yaml=row.config_yaml,
    )


# ── Reads ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[SourceSummary])
async def list_sources(
    session: _Session,
    origin: Literal["file", "api"] | None = Query(default=None),
) -> list[SourceSummary]:
    repo = SourcesRepository(session)
    origin_enum = SourceOrigin(origin) if origin is not None else None
    rows = await repo.list_all(origin=origin_enum)
    return [_summary(r) for r in rows]


@router.get("/{name}", response_model=SourceDetail)
async def get_source(name: str, session: _Session) -> SourceDetail:
    repo = SourcesRepository(session)
    row = await repo.get_by_name(name)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source {name!r} not found",
        )
    return _detail(row)


# ── Writes ───────────────────────────────────────────────────────────────────


@router.post("", response_model=SourceDetail, status_code=status.HTTP_201_CREATED)
async def create_source(body: SourceSubmission, session: _Session) -> SourceDetail:
    config, canonical = _parse_submission(body)
    repo = SourcesRepository(session)
    try:
        row = await repo.create(
            config=config,
            origin=SourceOrigin.api,
            yaml_text=canonical,
        )
    except DuplicateSourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    await session.commit()
    await session.refresh(row)
    return _detail(row)


@router.patch("/{name}", response_model=SourceDetail)
async def update_source(name: str, body: SourceSubmission, session: _Session) -> SourceDetail:
    config, canonical = _parse_submission(body)
    if config.name != name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Path name {name!r} does not match body name {config.name!r}",
        )
    repo = SourcesRepository(session)
    try:
        row = await repo.update_config(name=name, config=config, yaml_text=canonical)
    except SourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ImmutableSourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    await session.commit()
    await session.refresh(row)
    return _detail(row)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(name: str, session: _Session) -> None:
    repo = SourcesRepository(session)
    try:
        await repo.delete(name=name)
    except SourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ImmutableSourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    await session.commit()
