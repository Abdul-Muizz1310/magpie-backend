# Spec: GitHub Actions Workflows

## Goal

Three GitHub Actions workflows:
1. **CI** (`ci.yml`) — lint + test (against a real Postgres service) + Docker build on every push to `main` and every PR.
2. **Weekly scrape** (`nightly-scrape.yml`, name kept for history) — runs every file-origin source every Sunday 00:00 UTC via a matrix strategy.
3. **Heal-on-failure** (`heal-on-failure.yml`) — triggers when the weekly scrape fails; invokes `magpie.healer.run` to fix broken selectors.

## Workflows

### `ci.yml`

- **Trigger:** `push` to main, `pull_request` targeting main
- **Jobs:** `lint` (ruff check + ruff format --check + mypy) → `test` (pytest with a Postgres service, Playwright chromium pre-installed) → `build` (`docker build`)
- **uv is cached via `astral-sh/setup-uv@v3 enable-cache: true`.**

### `nightly-scrape.yml`

- **Trigger:** `schedule` (cron `0 0 * * 0` — weekly Sunday 00:00 UTC) + `workflow_dispatch` (manual)
- **Strategy:** a `discover` job reads every `configs/*.yaml`, extracts the names, and emits two JSON arrays (`sources`, `js_sources`) as job outputs. The downstream `run` job's matrix is `fromJson(needs.discover.outputs.sources)` with `fail-fast: false`, so adding a new YAML automatically adds a CI leg with no workflow edit needed.
- **Steps (per matrix leg):** checkout, setup python 3.12, install uv, sync deps, install Playwright chromium **only if the source appears in `js_sources`**, apply Alembic migrations, run `magpie run <source>`.
- **Secrets needed:** `DATABASE_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID`.

### `heal-on-failure.yml`

- **Trigger:** `workflow_run` on `nightly-scrape` with type `completed`, condition `conclusion == 'failure'`
- **Permissions:** `contents: write`, `pull-requests: write`
- **Steps:** checkout, setup python 3.12, install uv, sync deps, run `python -m magpie.healer.run` (walks the most-recent failed runs in Postgres and heals each source).
- **Secrets needed:** `OPENROUTER_API_KEY`, `PAT_SCRAPE_HEALER` (mapped to env `GITHUB_PAT_SCRAPE_HEALER`), `R2_*`, `DATABASE_URL`, plus the hardcoded `GITHUB_REPO` and `GITHUB_HEAL_LABEL` env vars.

## Invariants

- Playwright chromium is installed only for matrix entries that need it. If a new JS-rendered source is added, update the conditional in `nightly-scrape.yml` (or make it unconditional).
- Heal-on-failure runs only when the weekly scrape fails, not on success.
- Heal-on-failure has write permissions for creating PRs against file-origin configs; api-origin configs are patched in place in the DB and do not produce PRs.
- All secrets are passed via `secrets.*`, never hardcoded.

## Operational notes

- **Adding a new source** is a one-file change — drop a YAML under `configs/`. The `discover` job picks it up on the next workflow run and the matrix expands automatically. If the new config is `render: true`, Playwright chromium is installed for that leg only; no workflow edit required.
- **Manual trigger**: `gh workflow run nightly-scrape.yml --ref main` runs the whole matrix without waiting for Sunday.
- **Healer can re-attempt**: re-running `heal-on-failure` is safe — PRs against the same branch (`heal/{source}`) are idempotent; db-patches are first-wins and won't duplicate records.
