"""Request/response models for POST /api/scrape/* (spec 06-batch-scrape).

All models are frozen and reject unknown keys — illegal states are
unrepresentable at the Pydantic boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── Shared field constraints ────────────────────────────────────────────────

SourceSlug = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$"),
]

MaxItems = Annotated[int, Field(ge=1, le=100)]


# ── Request models ──────────────────────────────────────────────────────────


class ScrapeOnceRequest(BaseModel):
    """Body for POST /api/scrape/{source}/once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_items: MaxItems = 10


class ScrapeBatchRequest(BaseModel):
    """Body for POST /api/scrape/batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources: Annotated[
        tuple[SourceSlug, ...],
        Field(min_length=1, max_length=10),
    ]
    max_items_per_source: MaxItems = 10

    @model_validator(mode="after")
    def _sources_must_be_unique(self) -> ScrapeBatchRequest:
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must not contain duplicates")
        return self


# ── Response models ─────────────────────────────────────────────────────────


class ScrapeItem(BaseModel):
    """A single scraped item in the response payload.

    ``url`` is intentionally unconstrained — some sources yield valid items
    without a link (e.g. Wikipedia bullets that are plain-text announcements
    with no anchor). Rejecting those would crash the whole batch on data the
    user would reasonably want captured.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stable_id: str = Field(min_length=1)
    url: str = ""
    title: str = ""
    content_text: str = ""
    content_hash: str = Field(min_length=1)
    fetched_at: datetime
    html_snapshot_url: str | None = None


class ScrapeResult(BaseModel):
    """Result of a single-source scrape — shared by /once and /batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: SourceSlug
    scraped_at: datetime
    run_id: UUID
    items: tuple[ScrapeItem, ...]


class ScrapeFailure(BaseModel):
    """Per-source failure reported by /batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    error: str


class ScrapeBatchResponse(BaseModel):
    """Body for 200 OK of POST /api/scrape/batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: tuple[ScrapeResult, ...]
    failed: tuple[ScrapeFailure, ...]
