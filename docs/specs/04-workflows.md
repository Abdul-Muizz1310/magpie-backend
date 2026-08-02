# Spec: GitHub Actions Workflows

## Goal

Three GitHub Actions workflows:
1. **CI** (`ci.yml`) — lint + test (against a real Postgres service) + Docker build on every push to `main` and every PR.
2. **Scheduled scrape** (`nightly-scrape.yml`, name kept for history) — runs hourly; `magpie due` filters the matrix to the sources whose own per-source `schedule` cron fires in the current hour, so each source's declared cadence is honoured (6-hourly, daily, weekly, …) rather than one global cadence.
3. **Heal-on-failure** (`heal-on-failure.yml`) — triggers when the scrape workflow fails (a scrape leg exits non-zero on a hard error *or* on min-item underflow); invokes `magpie.healer.run`, which heals both failed runs and sources whose latest OK run underflowed `health.min_items`.

## Workflows

### `ci.yml`

- **Trigger:** `push` to main, `pull_request` targeting main
- **Jobs:** `lint` (ruff check + ruff format --check + mypy) → `test` (pytest with a Postgres service, Playwright chromium pre-installed) → `build` (`docker build`)
- **uv is cached via `astral-sh/setup-uv@v3 enable-cache: true`.**

### `nightly-scrape.yml`

- **Trigger:** `schedule` (cron `0 * * * *` — hourly) + `workflow_dispatch` (manual)
- **Strategy:** a `discover` job runs `magpie due --window-seconds 3600`, which reads every `configs/*.yaml` and emits two JSON arrays (`sources`, `js_sources`) containing only the sources whose per-source `schedule` cron fires within the last hour. Manual `workflow_dispatch` passes `--all` to bypass the filter and run every source. The downstream `run` job's matrix is `fromJson(needs.discover.outputs.sources)` with `fail-fast: false`; an empty array simply skips the `run` job that hour.
- **Steps (per matrix leg):** checkout, setup python 3.12, install uv, sync deps, install Playwright chromium **only if the source appears in `js_sources`**, apply Alembic migrations, run `magpie run <source>` (exits non-zero on min-item underflow, escalating to heal-on-failure).
- **Secrets needed:** `DATABASE_URL`. (No `R2_*`: pre-parse HTML archiving to Cloudflare R2 is planned, not shipped — see spec 01 — so no shipped module reads an `R2_*` variable and the workflow no longer exports them.)

### `heal-on-failure.yml`

- **Trigger:** `workflow_run` on `nightly-scrape` with type `completed`, condition `conclusion == 'failure'` (plus manual `workflow_dispatch`)
- **Permissions:** `contents: write`, `pull-requests: write`
- **Steps:** checkout, setup python 3.12, install uv, sync deps, run `python -m magpie.healer.run` (heals the most-recent failed runs *and* any source whose latest OK run underflowed `health.min_items`, so silent selector drift is caught too).
- **Secrets needed:** `OPENROUTER_API_KEY`, `PAT_SCRAPE_HEALER` (mapped to env `GITHUB_PAT_SCRAPE_HEALER`), `DATABASE_URL`, plus the hardcoded `GITHUB_REPO` and `GITHUB_HEAL_LABEL` env vars. No `R2_*` — the healer re-fetches the source's live HTML rather than reading an archived snapshot.

## Invariants

- Playwright chromium is installed only for matrix entries that need it. If a new JS-rendered source is added, update the conditional in `nightly-scrape.yml` (or make it unconditional).
- Heal-on-failure runs only when the scheduled scrape fails, not on success. (It is hourly, not weekly — the workflow's `nightly-scrape` filename is kept for history; see the cron above.)
- Heal-on-failure has write permissions for creating PRs against file-origin configs; api-origin configs are patched in place in the DB and do not produce PRs.
- All secrets are passed via `secrets.*`, never hardcoded.

## Operational notes

- **Adding a new source** is a one-file change — drop a YAML under `configs/`. The `discover` job picks it up on the next workflow run and the matrix expands automatically. If the new config is `render: true`, Playwright chromium is installed for that leg only; no workflow edit required.
- **Manual trigger**: `gh workflow run nightly-scrape.yml --ref main` passes `--all`, running every source immediately instead of only those whose per-source cron is due this hour.
- **Healer can re-attempt**: re-running `heal-on-failure` is safe — PRs against the same branch (`heal/{source}`) are idempotent; db-patches are first-wins and won't duplicate records.
