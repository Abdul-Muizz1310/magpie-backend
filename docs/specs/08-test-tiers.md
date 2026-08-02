# Spec 08 — Test tiers

## Goal

Make the test pyramid explicit and honest. Three tiers, each with a stated
dependency and a documented way to run or skip it:

| Tier | Marker | Dependency | Command |
|---|---|---|---|
| Fast | *(none)* | none — pure Python + SQLite + local fixture servers | `uv run pytest -m "not slow"` |
| Postgres integration | `slow` | a Docker daemon (Testcontainers starts `postgres:16-alpine`) | `uv run pytest -m slow` |
| Live smoke | `smoke` | a reachable deployment (`SMOKE_BASE_URL`) | `SMOKE_BASE_URL=... uv run pytest -m smoke` |

`uv run pytest` (no `-m`) runs every tier that its dependency allows and is what
CI's `test` job executes. `SMOKE_BASE_URL` is deliberately **not** set for that
job, so the smoke tier skips there; the live probe runs in CI's separate
push-gated `smoke` job, which passes `${{ vars.SMOKE_BASE_URL }}` and adds
`--no-cov` (two tests cannot clear a whole-project coverage gate).

`SMOKE_BASE_URL` is a repository **variable**, not a secret — the value is a
public URL, and secrets are unavailable to fork pull requests. Its value is a
bare origin: **no trailing slash and no path**. The tests append `/health` and
`/version`; a value containing `/health` would be a misconfiguration.

## Why a real Postgres tier

Everything under `tests/` ran against SQLite, which silently disagrees with
Postgres on the exact behaviours this schema depends on:

- **Foreign keys are not enforced** by SQLite unless `PRAGMA foreign_keys=ON` is
  issued per connection. `ondelete="CASCADE"` / `ondelete="SET NULL"` in
  `storage/models.py` were therefore never exercised.
- **`SELECT ... FOR UPDATE` is a no-op** on SQLite. `PgItemRepository.persist_items`
  takes that lock specifically to serialise concurrent persists of one source;
  on SQLite the lock does nothing, so the guard was untested.
- **Native `ENUM` types** (`source_origin`, `run_status`, `heal_mode`) are created
  as real Postgres types by the Alembic migration but degrade to `VARCHAR` +
  `CHECK` on SQLite, so out-of-domain values were never rejected by the DB.
- **`sa.Uuid` / `sa.JSON`** map to native `uuid` / `json` on Postgres and to
  `CHAR(32)` / `TEXT` on SQLite.
- The migration chain itself had only ever been applied to SQLite.

## Behaviour under test (Postgres tier)

1. `alembic upgrade head` against Postgres creates the four tables, the three
   native enum types, and `ix_items_source_removed_last_seen`; `downgrade base`
   removes the tables again.
2. `storage.db._normalize_url` turns a stock `postgresql://` URL into one that
   `asyncpg` connects with, and `check_db()` returns `True` through it.
3. `uq_items_source_dedupe` is enforced by the database, not just by the repo's
   in-batch pre-check.
4. Deleting a `sources` row cascades to its `items`, `runs`, and `heals`.
5. Deleting a `runs` row sets `heals.run_id` to `NULL` rather than failing.
6. A raw insert of an out-of-domain `runs.status` is rejected by the enum type.
7. `PgItemRepository.persist_items` produces the documented new / updated /
   removed / reappeared accounting on Postgres.
8. Two concurrent `persist_items` calls for the same source both complete
   without an integrity error and leave exactly one row per `dedupe_key`.
9. `PgRunRepository.mark_stale_running_as_error` reaps by timezone-aware
   comparison against `timestamptz`.
10. The viewer API (`/sources`, `/runs`, `/heals`, `/sources/{name}/items`)
    answers correctly when its session factory is bound to Postgres.

## Failure / edge cases

- [x] Docker daemon unreachable → the tier skips with a reason, it does not fail.
- [x] Each test gets its own freshly created database, so ordering cannot leak
      state between tests.
- [x] `check_db()` returns `False` (not raises) for an unreachable Postgres URL.

## Behaviour under test (live smoke tier)

1. `GET {SMOKE_BASE_URL}/health` returns 200 with `status: "ok"`,
   `service: "magpie"`, `db: "ok"`, and a `commit_sha`.
2. `GET {SMOKE_BASE_URL}/version` returns 200 and its `commit_sha` matches
   `/health`'s.
3. A 503 from `/health` fails the smoke test with the body echoed — a suspended
   or DB-less deployment is a real failure *when you asked for a smoke run*.
4. A cold-booting instance is retried, not failed. Render Free spins idle
   instances down; the first request afterwards was measured holding the
   connection open ~70s, and the proxy sometimes answers 502 meanwhile. Each
   probe therefore allows a 120s per-attempt timeout and 3 attempts, retrying
   transport errors immediately and retryable 5xx after 5s. Exhausting that
   budget *is* a failure — the tolerance is bounded, not a blanket `try/except`.

## Failure / edge cases

- [x] `SMOKE_BASE_URL` unset or blank → every smoke test skips **before any HTTP
      request is issued**, and the run exits 0. Free-tier deployments sleep,
      cold-boot and occasionally sit suspended returning 503; an unconditional
      live check would make CI red for a reason unrelated to the commit.
- [x] Non-`http(s)` `SMOKE_BASE_URL` → fail loudly rather than silently skip,
      so a typo'd variable isn't mistaken for a passing smoke run.
- [x] Trailing slash on `SMOKE_BASE_URL` → normalised away, so the probe URL
      joins with exactly one separator (`.../health`, never `...//health`).
- [x] Connection error / timeout → fail with the URL in the message.
