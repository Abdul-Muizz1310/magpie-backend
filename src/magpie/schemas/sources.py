"""Request/response models for custom-source CRUD."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourceSubmission(BaseModel):
    """POST /api/sources or PATCH /api/sources/{name} body.

    Either ``yaml`` (raw YAML text) or ``config`` (parsed dict) must be
    supplied. The service re-validates via ``SourceConfig`` either way, so
    invalid selectors still fail at 422.
    """

    model_config = ConfigDict(extra="forbid")

    yaml: str | None = None
    config: dict[str, Any] | None = None

    def is_valid(self) -> bool:
        return self.yaml is not None or self.config is not None


class SourceSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str
    description: str
    origin: Literal["file", "api"]
    config_sha: str
    created_at: datetime
    updated_at: datetime


class SourceDetail(SourceSummary):
    config_yaml: str = Field(description="Raw YAML as stored in Postgres.")
