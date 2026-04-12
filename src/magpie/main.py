"""FastAPI viewer API for magpie scraper data."""

from __future__ import annotations

import os
import re
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="magpie", description="YAML-defined scrapers that self-heal")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        os.environ.get("FRONTEND_URL", "https://magpie-frontend.vercel.app"),
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# In-memory store (replaced by DB in production)
_sources_store: list[dict[str, Any]] = []
_runs_store: list[dict[str, Any]] = []
_heals_store: list[dict[str, Any]] = []

_SOURCE_NAME_RE = re.compile(r"^[a-z0-9-]+$")


# ── Response models ──────────────────────────────────────────────────────────


class SourceResponse(BaseModel):
    name: str
    description: str = ""
    last_run_at: str | None = None
    last_status: str | None = None
    item_count: int = 0
    config_sha: str = ""


class RunResponse(BaseModel):
    id: int
    source: str
    started_at: str
    ended_at: str | None = None
    items_new: int = 0
    items_updated: int = 0
    items_removed: int = 0
    status: str = ""
    error: str | None = None


class HealResponse(BaseModel):
    id: int
    source: str
    run_id: int | None = None
    old_config: dict[str, Any] = {}
    new_config: dict[str, Any] = {}
    pr_url: str | None = None
    created_at: str = ""


class HealthResponse(BaseModel):
    status: str
    version: str = ""
    db: str = "ok"


# ── Endpoints ────────────────────────────────────────────────────────────────


@app.get("/sources", response_model=list[SourceResponse])
async def list_sources() -> list[dict[str, Any]]:
    """List all configured sources with latest status."""
    return _sources_store


@app.get("/sources/{name}", response_model=SourceResponse)
async def get_source(name: str) -> dict[str, Any]:
    """Get a single source by name."""
    if not _SOURCE_NAME_RE.match(name):
        raise HTTPException(status_code=422, detail="Invalid source name format")
    for source in _sources_store:
        if source["name"] == name:
            return source
    raise HTTPException(status_code=404, detail="source not found")


@app.get("/runs", response_model=list[RunResponse])
async def list_runs(
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    """List run history, optionally filtered by source."""
    runs = _runs_store
    if source:
        runs = [r for r in runs if r["source"] == source]
    # Sort by started_at desc
    runs = sorted(runs, key=lambda r: r.get("started_at", ""), reverse=True)
    return runs[offset : offset + limit]


@app.get("/heals", response_model=list[HealResponse])
async def list_heals(
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    """List heal history with PR links."""
    heals = _heals_store
    if source:
        heals = [h for h in heals if h["source"] == source]
    heals = sorted(heals, key=lambda h: h.get("created_at", ""), reverse=True)
    return heals[offset : offset + limit]


@app.get("/health", response_model=HealthResponse)
async def health() -> dict[str, str]:
    """Health check endpoint."""
    db_status = "ok"
    try:
        from magpie.storage.db import check_db

        if not await check_db():
            db_status = "down"
    except Exception:
        db_status = "down"

    return {
        "status": "ok",
        "version": os.environ.get("COMMIT_SHA", "dev"),
        "db": db_status,
    }


@app.get("/version")
async def version() -> dict[str, str]:
    """Return the commit SHA."""
    return {"version": os.environ.get("COMMIT_SHA", "dev")}
