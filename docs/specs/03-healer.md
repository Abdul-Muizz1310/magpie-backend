# Spec: Self-Healing via LLM + GitHub PR

## Goal

When a scrape returns fewer items than `health.min_items`, automatically attempt to fix the broken CSS selector by asking an LLM to derive a new one from the raw HTML snapshot. If the LLM's proposed selector works against the snapshot, open a GitHub PR with the updated YAML config. No auto-merge — human reviews.

## Components

1. **Detector** (`healer/detector.py`) — evaluates whether a run's item count triggers healing
2. **Selector Fixer** (`healer/selector_fixer.py`) — calls OpenRouter LLM with the old selector + raw HTML, gets a new selector proposal
3. **Validator** (`healer/validator.py`) — runs the proposed selector against the HTML snapshot to verify it extracts items
4. **GitHub PR** (`healer/github_pr.py`) — creates a branch, modifies the YAML, opens a PR with diff + reasoning

## Inputs

- A completed `Run` with `items_new + items_updated < health.min_items`
- The `SourceConfig` for the source
- Raw HTML snapshot from R2 (archived by the scraper before parsing)

## Outputs

- A GitHub PR modifying the YAML config file, labeled `scrape:self-heal`
- PR body contains: old selector, new selector, LLM confidence, reasoning, sample extracted values
- Or: no PR if the LLM cannot find a valid selector (logged as error)

## Invariants

- Healer only fires when `item_count < health.min_items` AND `health.min_items > 0`
- Healer never auto-merges — PR requires human review
- Healer checks for an existing open PR on the same source before creating a new one; updates the existing branch instead of spamming PRs
- LLM response must be valid JSON matching `{selector, confidence, reasoning, sample_values}`
- Proposed selector must extract >= 1 item from the snapshot to be accepted
- If selector is null or extracts 0 items, no PR is created (error logged)
- LLM call uses OpenRouter with the model from `OPENROUTER_MODEL_PRIMARY`
- LLM prompt is loaded from `healer/prompts/fix_selector.md` (file-based, not inline)
- PR branch name: `heal/<source-name>-<timestamp>`
- PR label: value of `GITHUB_HEAL_LABEL` env var (default `scrape:self-heal`)
- GitHub operations use `GITHUB_PAT_SCRAPE_HEALER` token (fine-grained, contents:write + PRs:write)

## Test cases

### Happy path
- [ ] Run with 0 items and min_items=20 triggers healer
- [ ] LLM returns valid JSON with a working selector — PR created
- [ ] PR body contains old selector, new selector, confidence, reasoning, and sample values
- [ ] PR is labeled with `scrape:self-heal`
- [ ] PR modifies only the broken field's selector in the YAML, rest of config unchanged
- [ ] Validator confirms proposed selector extracts >= 1 item from snapshot

### Edge cases
- [ ] Multiple broken fields in same config — healer attempts each field independently
- [ ] Existing open PR for same source — existing branch updated, no duplicate PR
- [ ] LLM returns selector with confidence < 0.5 — PR created but body includes low-confidence warning
- [ ] HTML snapshot is very large (>20K chars) — truncated to 20K in prompt
- [ ] Source with render=true — healer still works (uses parsel on raw HTML, not Playwright)

### Failure cases
- [ ] LLM returns selector=null — no PR created, error logged with reasoning
- [ ] LLM returns invalid JSON — retry up to 3 times, then log error, no PR
- [ ] LLM returns selector that extracts 0 items from snapshot — no PR, error logged
- [ ] OpenRouter API returns 429 (rate limit) — exponential backoff, 3 retries
- [ ] OpenRouter API returns 500 — retry with backoff, then log error
- [ ] GitHub API returns 403 (bad token) — clear error message about token permissions
- [ ] GitHub API returns 422 (branch already exists) — update existing branch
- [ ] R2 snapshot not found — log error, skip healing for this source
- [ ] health.min_items=0 — healer never triggers regardless of item count
- [ ] Run with items >= min_items — healer does not trigger

## Acceptance criteria

- [ ] All test cases pass
- [ ] LLM calls mocked in unit tests via respx (deterministic, no real API calls)
- [ ] GitHub API calls mocked in unit tests
- [ ] Integration test: broken fixture HTML -> healer runs -> PR body matches expected snapshot
- [ ] Prompt lives in `healer/prompts/fix_selector.md`, not inline
- [ ] No secrets logged at any point
