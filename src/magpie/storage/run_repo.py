"""In-memory run repository — mirrors the shape of the nightly-cron runs table.

Persisting every on-demand scrape here lets the existing ``/runs`` endpoint
show interactive runs alongside cron-scheduled ones without re-plumbing the
demo-seeded store. A Postgres-backed implementation can swap in behind the
same interface later without changing the service layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RunRecord:
    """A single row of the logical ``runs`` table."""

    run_id: str
    source: str
    started_at: datetime
    ended_at: datetime
    item_count: int
    duration_ms: int
    status: str  # "ok" | "error"
    error: str | None
    items_new: int = 0
    items_updated: int = 0
    items_removed: int = 0


class RunRepository:
    """Append-only store of run records."""

    def __init__(self) -> None:
        self._rows: list[RunRecord] = []

    def record_run(self, row: RunRecord) -> None:
        self._rows.append(row)

    def list_runs(self) -> list[RunRecord]:
        # Returning a copy prevents accidental mutation by callers.
        return list(self._rows)
