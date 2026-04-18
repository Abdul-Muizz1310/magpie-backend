# Spec: Batch Scrape API (on-demand scrape endpoints)

## Goal

Expose two new FastAPI endpoints that allow callers (the bastion gateway → dossier
pipeline, operators) to trigger a registered scraper synchronously and receive the
scraped items in the response. The existing CLI/cron path is preserved: these are
additive HTTP routes that dispatch through the same spider factory and repository
layer so `/runs` continues to reflect every execution.

## Endpoints

### `POST /api/scrape/{source}/once`

Run **one** registered scraper on demand and return its items.

- **Path param** `source: str` — must match a registered YAML source slug
  (`^[a-z0-9-]+$`). Unknown source → **404** with
  `{"detail": "Unknown source: <name>"}`. Malformed slug → **404** via the same
  error surface (no leakage of internal ids).
- **Request body** (all fields optional):
  ```json
  { "max_items": 10 }
  ```
  - `max_items: int` — default 10, `ge=1`, `le=100`. Enforced by Pydantic; illegal
    values → **422**.
- **Response 200:**
  ```json
  {
    "source": "hackernews",
    "scraped_at": "2026-04-18T14:22:05.123456+00:00",
    "run_id": "8f1c3e12-5b3a-4e41-9c2b-01a2cfb8d0a1",
    "items": [
      {
        "stable_id": "<sha256>",
        "url": "https://...",
        "title": "...",
        "content_text": "...",
        "content_hash": "<sha256 of NFC-normalised content_text>",
        "fetched_at": "2026-04-18T14:22:04.999999+00:00",
        "html_snapshot_url": null
      }
    ]
  }
  ```
  - `stable_id` = the content-derived fingerprint produced by the scraper's
    `dedupe_key` field (matches the existing hashing path — no new id scheme).
  - `html_snapshot_url` = R2 URL if a snapshot was captured, else `null`.
  - `content_hash` = SHA-256 of NFC-normalised `content_text`.
  - `scraped_at`, `fetched_at` are timezone-aware UTC (Pydantic serialises
    `datetime` as ISO-8601 with offset).
- **Errors:**
  - **404** unknown source.
  - **422** invalid body (Pydantic).
  - **503** scraping failed hard (typed `ScrapeExecutionError` bubbled out of the
    service layer).
  - **504** internal timeout > 60s.

### `POST /api/scrape/batch`

Run multiple registered scrapers concurrently.

- **Request body:**
  ```json
  { "sources": ["hackernews", "arxiv-cs"], "max_items_per_source": 10 }
  ```
  - `sources: list[str]` — `min_length=1`, `max_length=10`, each slug-shaped and
    **unique** (duplicates rejected in a `model_validator`).
  - `max_items_per_source: int` — default 10, `ge=1`, `le=100`.
- **Response 200:**
  ```json
  {
    "runs":   [ <same shape as /once response>, ... ],
    "failed": [ { "source": "broken", "error": "..." } ]
  }
  ```
  - Per-source scrapes dispatched via `asyncio.gather(..., return_exceptions=True)`.
    One failure isolates to `failed[]`; successful sources still appear in `runs[]`.
  - Unknown source in the list is reported as a per-source failure in `failed[]`
    (does **not** 404 the whole batch).
- **Errors:**
  - **422** empty `sources`, duplicates, >10 entries, malformed slug, or
    `max_items_per_source` out of range.

## Security

- These endpoints are **not auth-gated inside magpie**. The bastion gateway
  terminates JWT / session auth and only trusted, authenticated callers ever
  reach this service. Magpie remains a downstream trusted component.
- Responses never expose DB-internal surrogate ids. Only `stable_id`
  (content-derived) and `run_id` (random UUIDv4) leave the service boundary.

## Layering

- **Router** `src/magpie/api/routers/scrape.py` — pure translation: validate
  body, resolve path param, call service, map typed result → response. No DB,
  no scraping, no business logic.
- **Service** `src/magpie/services/scrape_service.py` — orchestration:
    1. look up the registered `SourceConfig` via the config registry;
    2. dispatch to `create_scraper()` (Scrapy class vs `PlaywrightRunner`);
    3. execute the scrape bounded by `max_items`;
    4. persist a `runs` record + items via `ItemRepository`;
    5. return a frozen `ScrapeResult` DTO.
  - Typed exceptions at the boundary: `UnknownSourceError`,
    `ScrapeExecutionError`. No broad `except Exception: pass`.
- **Schemas** `src/magpie/schemas/scrape.py` — frozen Pydantic v2 request /
  response models, `extra="forbid"`, constrained field types (`ge=1, le=100`,
  regex-constrained slugs).
- **Storage** — reuse existing `ItemRepository` + add a `RunRepository` to
  persist run metadata (the existing in-memory `_runs_store` in `main.py` is
  demo-seeded; the new repo writes the real runs path that the spec requires).

## Invariants

- Illegal states are unrepresentable: `max_items`, `max_items_per_source` are
  type-bounded by Pydantic; `sources` is length-bounded and unique; slug shape
  is enforced by pattern.
- Request/response models are **frozen** (`ConfigDict(frozen=True,
  extra="forbid")`).
- `content_text` is NFC-normalised **before** hashing. Hashing is pure and
  deterministic (SHA-256 over the NFC string).
- A scrape **always** writes a `runs` row, success or failure, so `/runs` sees
  every execution.
- Batch concurrency preserves isolation: one source's exception can never abort
  the whole batch.
- Router layer **never** touches the DB or the scraper runners directly.

## Enumerated test cases

Router-level (`tests/unit/api/test_scrape_router.py`):

Pass:
1. `POST /api/scrape/hackernews/once` with valid body → 200, response shape
   matches spec, ≥1 item (service mocked to return canned items).
2. `POST /api/scrape/hackernews/once` with `max_items=5` → returned items ≤ 5.
3. `POST /api/scrape/hackernews/once` with empty body `{}` → defaults
   (`max_items=10`) applied.
4. `POST /api/scrape/batch` with 2 valid sources → 200, `runs` has 2 entries.
5. `POST /api/scrape/batch` with all failing sources → 200, empty `runs`,
   `failed[]` lists each.
6. `POST /api/scrape/batch` mixed → partial: success in `runs`, failures in
   `failed`.
7. All responses include `scraped_at` as a timezone-aware UTC datetime.
8. Response `run_id` is a valid UUID (v4).

Fail:
9. `POST /api/scrape/nonexistent/once` → 404,
   `{"detail": "Unknown source: nonexistent"}`.
10. `{"max_items": 0}` → 422.
11. `{"max_items": 101}` → 422.
12. `{"sources": []}` → 422.
13. 11 sources → 422.
14. Duplicate sources → 422.
15. Slug-violating source in path (e.g. `Bad%20Name`) → 404 (unknown
    source — our registry only holds slugged names; matches the "no leakage"
    invariant).

Security:
16. Unauthenticated requests pass — no auth check at this layer (bastion's
    responsibility). Documented, not enforced.
17. Response body exposes `stable_id` + `run_id`, never DB internal ids.

Service-level (`tests/unit/services/test_scrape_service.py`):
18. `scrape_once(source, max_items)` dispatches to the correct runner based on
    `render` field — static config → Scrapy class, JS config → PlaywrightRunner.
19. Service writes a `runs` row (status, item_count, duration) via
    `RunRepository`.
20. `content_text` is NFC-normalised before `content_hash` is computed.
21. Empty items list returns gracefully (no crash, `items: []`).
22. `scrape_batch` uses `asyncio.gather(return_exceptions=True)`; middle
    source raising does not abort the other two.

## Acceptance criteria

- [ ] Spec doc committed.
- [ ] 22 new test cases added, all green.
- [ ] 135 existing tests still green.
- [ ] 100 % line coverage preserved.
- [ ] `ruff check .` clean.
- [ ] `mypy` (strict) clean.
- [ ] Router registered via `app.include_router(...)` in `main.py`.
- [ ] No auth middleware added to magpie (bastion is the boundary).
