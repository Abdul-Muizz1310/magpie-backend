# Spec: Viewer API (FastAPI)

## Goal

A read-only FastAPI application exposing three endpoints for the magpie-frontend to consume: `/sources` (list all configured sources with status), `/runs` (run history for a source), and `/heals` (heal history with PR links). Plus standard platform endpoints: `/health`, `/version`.

## Endpoints

### `GET /sources`

Returns all sources with their latest run status and item count.

**Response:**
```json
[
  {
    "name": "hackernews",
    "description": "Scrape Hacker News front page",
    "last_run_at": "2026-04-12T09:00:00Z",
    "last_status": "ok",
    "item_count": 30,
    "config_sha": "abc123"
  }
]
```

### `GET /sources/{name}`

Returns a single source with its config details and latest items.

**Response:** source object + `items: [...]` (latest non-removed items, paginated)

### `GET /runs?source={name}&limit={n}`

Returns run history for a source, ordered by `started_at desc`.

**Response:**
```json
[
  {
    "id": 1,
    "source": "hackernews",
    "started_at": "2026-04-12T09:00:00Z",
    "ended_at": "2026-04-12T09:00:15Z",
    "items_new": 2,
    "items_updated": 1,
    "items_removed": 0,
    "status": "ok",
    "error": null
  }
]
```

### `GET /heals?source={name}&limit={n}`

Returns heal history with PR links.

**Response:**
```json
[
  {
    "id": 1,
    "source": "hackernews",
    "run_id": 42,
    "old_config": {"selector": "span.old"},
    "new_config": {"selector": "span.new"},
    "pr_url": "https://github.com/Abdul-Muizz1310/magpie-backend/pull/3",
    "created_at": "2026-04-12T10:00:00Z"
  }
]
```

### `GET /health`

Returns `{"status": "ok", "version": "<commit_sha>", "db": "ok"|"down"}`.

### `GET /version`

Returns the commit SHA baked in at build time.

## Invariants

- All endpoints are read-only (GET only)
- Source name validated against `^[a-z0-9-]+$` pattern in path params
- Pagination via `limit` (default 20, max 100) and `offset` (default 0) query params
- Responses use Pydantic response models (no raw dicts)
- CORS configured for magpie-frontend origin + localhost:3000
- Invalid source name returns 404 with `{"detail": "source not found"}`
- DB errors return 503 with `{"detail": "database unavailable"}`
- `/health` never crashes — returns `db: "down"` on connection failure

## Test cases

### Happy path
- [ ] `GET /sources` returns list of all sources with correct fields
- [ ] `GET /sources/hackernews` returns source details + items
- [ ] `GET /runs?source=hackernews` returns run history ordered by started_at desc
- [ ] `GET /runs?source=hackernews&limit=5` returns at most 5 runs
- [ ] `GET /heals?source=hackernews` returns heal history with PR URLs
- [ ] `GET /health` returns status ok with db ok when connected
- [ ] `GET /version` returns a commit SHA string

### Edge cases
- [ ] `GET /sources` with no sources in DB returns empty list (not error)
- [ ] `GET /runs?source=hackernews` with no runs returns empty list
- [ ] `GET /heals` without source param returns all heals across sources
- [ ] `GET /runs?limit=0` returns 422 validation error
- [ ] `GET /runs?limit=200` clamped to max 100

### Failure cases
- [ ] `GET /sources/INVALID_NAME` returns 422 (pattern mismatch)
- [ ] `GET /sources/nonexistent` returns 404
- [ ] DB connection down — `/health` returns `{"status": "ok", "db": "down"}`, does not crash
- [ ] DB connection down — `/sources` returns 503

## Acceptance criteria

- [ ] All test cases pass
- [ ] Response models defined as Pydantic schemas in `schemas/` module
- [ ] No raw SQL — all queries via SQLAlchemy async
- [ ] CORS middleware configured
- [ ] `/health` endpoint always responds (even with DB down)
- [ ] Integration tests use Testcontainers postgres
