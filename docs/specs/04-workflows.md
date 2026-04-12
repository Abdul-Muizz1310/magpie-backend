# Spec: GitHub Actions Workflows

## Goal

Two GitHub Actions workflows automate scraping and healing:
1. **Nightly scrape** — runs all configured sources on a cron schedule (every 6 hours) via a matrix strategy
2. **Heal-on-failure** — triggers after a nightly scrape fails, runs the healer on sources that produced zero items

## Workflows

### `nightly-scrape.yml`

- **Trigger:** `schedule` (cron `0 */6 * * *`) + `workflow_dispatch` (manual, optional source name)
- **Strategy:** matrix over source names `[hackernews, arxiv-cs, weather-live]` (not `demo-broken`)
- **Steps:** checkout, setup python 3.12, install uv, sync deps, install Playwright chromium (only for JS sources), run `scrape run <source>`
- **Secrets needed:** `DATABASE_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`

### `heal-on-failure.yml`

- **Trigger:** `workflow_run` on `nightly-scrape` with type `completed`, condition `conclusion == 'failure'`
- **Permissions:** `contents: write`, `pull-requests: write`
- **Steps:** checkout, setup python 3.12, install uv, sync deps, run `python -m magpie.healer.run`
- **Secrets needed:** `OPENROUTER_API_KEY`, `GITHUB_PAT_SCRAPE_HEALER`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `DATABASE_URL`

## Invariants

- `demo-broken` is excluded from nightly scrape (it exists only for demo purposes)
- Playwright chromium installed only for matrix entries that need it (weather-live)
- Healer workflow only runs when nightly scrape fails, not on success
- Healer workflow has write permissions for creating PRs
- All secrets are passed via `secrets.*`, never hardcoded

## Test cases

### Happy path
- [ ] Nightly scrape workflow YAML is valid (passes `actionlint` or manual review)
- [ ] Heal-on-failure workflow YAML is valid
- [ ] Matrix correctly lists all production sources (not demo-broken)
- [ ] Playwright install step has correct `if` condition for JS sources
- [ ] workflow_dispatch input allows running a single source manually

### Edge cases
- [ ] Manual dispatch with blank source name runs all sources
- [ ] Nightly scrape succeeds but one matrix job fails — heal-on-failure triggers

### Failure cases
- [ ] Missing secrets cause clear error messages (not silent failures)
- [ ] Playwright install failure on non-JS source does not block other matrix jobs

## Acceptance criteria

- [ ] Both workflow files committed and valid YAML
- [ ] Nightly scrape runs green on main with real sources (verified after S5 deploy)
- [ ] Heal-on-failure triggers correctly on scrape failure (verified via demo-broken in S5)
- [ ] No secrets hardcoded in workflow files
