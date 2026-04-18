"""Request/response models for the async-job endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EnqueueResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    job_id: str | None
    source: str
    status: Literal["queued"]


class RunView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    source: str
    status: Literal["queued", "running", "ok", "error"]
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int
    item_count: int
    items_new: int
    items_updated: int
    items_removed: int
    error: str | None = None
    job_id: str | None = None


class RunItemView(BaseModel):
    """A single item persisted during a run's time window.

    Built from the ``items`` table (not the raw scrape payload), so fields can
    be empty when the source's config does not populate them. The frontend
    renders whichever fields are present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    stable_id: str
    url: str
    title: str
    content_text: str
    content_hash: str
    first_seen_at: datetime
    last_seen_at: datetime
    html_snapshot_url: str | None = None
