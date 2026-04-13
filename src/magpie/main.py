"""FastAPI viewer API for magpie scraper data."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from magpie.platform.health import install_health_routes
from magpie.platform.middleware import install_middleware

app = FastAPI(title="magpie", description="YAML-defined scrapers that self-heal")
install_middleware(app)
install_health_routes(app)

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


# ── Data loading ────────────────────────────────────────────────────────────


def _load_configs() -> list[dict[str, Any]]:
    """Load source configs from YAML files in the configs/ directory."""
    configs_dir = Path(__file__).resolve().parent.parent.parent / "configs"
    sources: list[dict[str, Any]] = []

    if not configs_dir.is_dir():
        return sources

    for yaml_file in sorted(configs_dir.glob("*.yaml")):
        try:
            raw = yaml_file.read_text(encoding="utf-8")
            config = yaml.safe_load(raw)
            if not isinstance(config, dict) or "name" not in config:
                continue
            sha = hashlib.sha256(raw.encode()).hexdigest()[:12]
            sources.append(
                {
                    "name": config["name"],
                    "description": config.get("description", ""),
                    "config_sha": sha,
                }
            )
        except Exception:
            continue

    return sources


def _generate_demo_data(
    sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate realistic demo run and heal data for loaded sources."""
    now = datetime.now(UTC)
    enriched_sources: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    heals: list[dict[str, Any]] = []
    run_id = 1

    for source in sources:
        name = source["name"]
        is_broken = name == "demo-broken"

        # Generate 3-5 runs per source over the last few days
        num_runs = 3 if is_broken else 5
        source_runs: list[dict[str, Any]] = []

        for i in range(num_runs):
            started = now - timedelta(hours=6 * (num_runs - i), minutes=2)
            ended = started + timedelta(seconds=45 + i * 10)

            if is_broken and i == num_runs - 1:
                # Latest run for broken config: 0 items, error
                run = {
                    "id": run_id,
                    "source": name,
                    "started_at": started.isoformat(),
                    "ended_at": ended.isoformat(),
                    "items_new": 0,
                    "items_updated": 0,
                    "items_removed": 0,
                    "status": "error",
                    "error": (
                        "0 items extracted — selector"
                        " span.nonexistent-class > a::text returned no matches"
                    ),
                }
            elif is_broken:
                run = {
                    "id": run_id,
                    "source": name,
                    "started_at": started.isoformat(),
                    "ended_at": ended.isoformat(),
                    "items_new": 0,
                    "items_updated": 0,
                    "items_removed": 0,
                    "status": "error",
                    "error": "0 items extracted — selector mismatch",
                }
            else:
                new_items = 30 - i * 3 if i == 0 else max(2, 8 - i)
                run = {
                    "id": run_id,
                    "source": name,
                    "started_at": started.isoformat(),
                    "ended_at": ended.isoformat(),
                    "items_new": new_items,
                    "items_updated": i * 2,
                    "items_removed": max(0, i - 2),
                    "status": "ok",
                    "error": None,
                }

            source_runs.append(run)
            runs.append(run)
            run_id += 1

        # Determine latest status
        latest_run = source_runs[-1]
        total_items = sum(r["items_new"] for r in source_runs)

        last_status = latest_run["status"]
        if name == "demo-broken":
            last_status = "healed"

        enriched_sources.append(
            {
                **source,
                "last_run_at": latest_run["started_at"],
                "last_status": last_status,
                "item_count": total_items,
            }
        )

    # Generate a heal for the demo-broken source
    heals.append(
        {
            "id": 1,
            "source": "demo-broken",
            "run_id": runs[-1]["id"] if runs else None,
            "old_config": {
                "field": "title",
                "selector": "span.nonexistent-class > a::text",
            },
            "new_config": {
                "field": "title",
                "selector": "span.titleline > a::text",
            },
            "pr_url": "https://github.com/Abdul-Muizz1310/magpie-backend/pull/1",
            "created_at": (now - timedelta(hours=1)).isoformat(),
        }
    )

    return enriched_sources, runs, heals


# ── Initialize data at startup ──────────────────────────────────────────────

_raw_sources = _load_configs()
_sources_store, _runs_store, _heals_store = _generate_demo_data(_raw_sources)


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



# Health and version endpoints provided by platform/health.py via install_health_routes(app).
