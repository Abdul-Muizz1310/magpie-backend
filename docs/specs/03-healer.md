# Spec: Self-Healing via LLM + GitHub PR

## Goal

When a scrape returns fewer items than `health.min_items`, automatically attempt to fix the broken CSS selector by asking an LLM to derive a new one from the source's current HTML. If the LLM's proposed selector works against that HTML, open a GitHub PR with the updated YAML config. No auto-merge — human reviews.

## Components

1. **Detector** (`healer/detector.py`) — evaluates whether a run's item count triggers healing
2. **Selector Fixer** (`healer/selector_fixer.py`) — calls OpenRouter LLM with the old selector + raw HTML, gets a new selector proposal
3. **Validator** (`healer/validator.py`) — runs the proposed selector against the re-fetched HTML to verify it extracts items
4. **GitHub PR** (`healer/github_pr.py`) — creates a branch, modifies the YAML, opens a PR with diff + reasoning

## Inputs

- A completed `Run` with `items_new + items_updated < health.min_items`
- The `SourceConfig` for the source
- The source's current HTML, re-fetched at heal time by `healer/apply._fetch_html`
  (Playwright for `render: true`, SSRF-guarded httpx otherwise).
  **Not** an R2 snapshot: archiving pre-parse HTML to R2 is planned, not shipped
  (see spec 01). Nothing in the repo writes to R2.

## Outputs

- A GitHub PR modifying the YAML config file, labeled `scrape:self-heal`
- PR body contains: old selector, new selector, LLM confidence, reasoning, sample extracted values
- Or: no PR if the LLM cannot find a valid selector (logged as error)

## Invariants

- Healer only fires when `item_count < health.min_items` AND `health.min_items > 0`
- Healer never auto-merges — PR requires human review
- Healer checks for an existing open PR on the same source before creating a new one; updates the existing branch instead of spamming PRs
- LLM response must be valid JSON matching `{selector, confidence, reasoning, sample_values}`
- Proposed selector must extract >= 1 item from the re-fetched HTML to be accepted
- If selector is null or extracts 0 items, no PR is created (error logged)
- LLM call uses OpenRouter with the model from `OPENROUTER_MODEL_PRIMARY` (defaults to the free-tier slug in code, never a paid model)
- On transient LLM errors (429/5xx) the fixer retries up to 3 times with exponential backoff, honouring `Retry-After` on 429
- Before prompting, the page HTML is stripped of `<script>`/`<style>`/comments and windowed around the broken selector so large pages don't blind the LLM
- LLM prompt is currently built inline in `selector_fixer._call_llm` (a `healer/prompts/` file is not yet used)
- PR branch name: `heal/<source-name>` (idempotent — one heal branch/PR per source, updated in place rather than spamming new PRs)
- Branch + commit are created via the GitHub Git Data API (blob → tree → commit → ref) so the patched `configs/<source>.yaml` actually lands on the branch before the PR is opened
- PR label: value of `GITHUB_HEAL_LABEL` env var (default `scrape:self-heal`)
- GitHub operations use `GITHUB_PAT_SCRAPE_HEALER` token (fine-grained, contents:write + PRs:write)

## Test cases

### Happy path
- [ ] Run with 0 items and min_items=20 triggers healer
- [ ] LLM returns valid JSON with a working selector — PR created
- [ ] PR body contains old selector, new selector, confidence, reasoning, and sample values
- [ ] PR is labeled with `scrape:self-heal`
- [ ] PR modifies only the broken field's selector in the YAML, rest of config unchanged
- [ ] Validator confirms proposed selector extracts >= 1 item from the re-fetched HTML

### Edge cases
- [ ] Multiple broken fields in same config — healer attempts each field independently
- [ ] Existing open PR for same source — existing branch updated, no duplicate PR
- [ ] LLM returns selector with confidence < 0.5 — PR created but body includes low-confidence warning
- [ ] Re-fetched HTML is very large — stripped and windowed, then capped at
      `selector_fixer.MAX_HTML_LENGTH` in the prompt
- [ ] Source with render=true — healer still works (uses parsel on raw HTML, not Playwright)

### Failure cases
- [ ] LLM returns selector=null — no PR created, error logged with reasoning
- [ ] LLM returns invalid JSON — retry up to 3 times, then log error, no PR
- [ ] LLM returns selector that extracts 0 items from the re-fetched HTML — no PR, error logged
- [ ] OpenRouter API returns 429 (rate limit) — exponential backoff, 3 retries
- [ ] OpenRouter API returns 500 — retry with backoff, then log error
- [ ] GitHub API returns 403 (bad token) — clear error message about token permissions
- [ ] GitHub API returns 422 (branch already exists) — update existing branch
- [ ] Live re-fetch fails (network error / non-2xx) — log error, skip healing for this source
- [ ] health.min_items=0 — healer never triggers regardless of item count
- [ ] Run with items >= min_items — healer does not trigger

## Acceptance criteria

- [ ] All test cases pass
- [ ] LLM calls mocked in unit tests via respx (deterministic, no real API calls)
- [ ] GitHub API calls mocked in unit tests
- [ ] Integration test: broken fixture HTML -> healer runs -> PR body matches the expected text
- [ ] Prompt lives in `healer/prompts/fix_selector.md`, not inline
- [ ] No secrets logged at any point

## Default model (2026-10-20)

The code default is the portfolio's primary free model, `google/gemma-4-26b-a4b-it:free`
(`selector_fixer.DEFAULT_MODEL`). The previous default, `nvidia/nemotron-nano-9b-v2:free`, was
withdrawn from OpenRouter: every heal call answered 404 and every heal job still reported success.
`render.yaml` and `heal-on-failure.yml` set `OPENROUTER_MODEL_PRIMARY` to the same slug, and a test
holds the three equal so a later swap has to change all of them.

## A heal ends healed or failed, never silent (2026-10-20)

On 2026-10-19 the heal job ran against the real `arxiv-cs` underflow, every OpenRouter call answered
404, and the job finished green with `{'healed': []}`. A heal that does not land must be a failure
the job reports, not a log line inside a green run.

`heal_source` returns a `HealSummary` (`healer/outcome.py`, frozen): `source`, `origin` (absent when
the source is unknown), `healed` (one entry per target whose fix landed: the PR opened for a
file-origin source, the stored config patched for an api-origin one) and `failures` (one entry per
target that did not, with a reason). A summary with neither is not constructible.
`succeeded` is true iff `failures` is empty, which means `healed` is not.

| What happened | `target` | `reason` starts with |
|---|---|---|
| source unknown | `source` | `source not found` |
| live re-fetch failed | `source` | `fetch failed:` |
| the live page extracts items and no field is empty everywhere | `source` | `nothing to heal:` |
| the LLM call raised after its retries (OpenRouter 404, 429, 5xx, bad JSON) | the target | `LLM error:` |
| the LLM proposed no selector | the target | `no selector proposed` |
| the proposed selector extracts nothing from the live page | the target | `selector rejected:` |
| the proposed selector does not compile as the CSS or XPath it claims to be | the target | `selector rejected:` |
| file-origin: the selector validated but the PR was not opened | the target | `PR not opened` |

Each failure is logged at ERROR where it happens; no secret and no token is logged.

**The heal job** (`python -m magpie.healer.run`, run by `heal-on-failure.yml`) logs every failed
summary at ERROR and exits **1** when any summary failed. It exits 0 only when every target healed,
or when there was no target. A manual dispatch (`HEAL_SOURCE_FILTER`) follows the same rule for its
one source.

**Targets.** A source is a target iff its *latest* run is `error`, or is `ok` with
`item_count < health.min_items` (and `min_items > 0`). Previously every source with any `error` run
in its history was a target, so a source that failed once weeks ago was re-healed on every run
(the 2026-10-19 job healed seven sources for one `arxiv-cs` underflow). With "nothing to heal" now a
failure, that would have turned every heal job red.

**The queue task** (`heal_source_task`) logs a failed summary at ERROR and returns its JSON dump. It
does not raise: Procrastinate's retry would repeat the LLM spend for the same outcome.

**GitHub failures name themselves.** Each step of `create_heal_pr` that fails (base ref, base
commit, blob, tree, commit, ref, PR) logs at ERROR the step, the HTTP status and GitHub's `message`,
never the token. A 401/403 adds that the `GITHUB_PAT_SCRAPE_HEALER` token lacks access.

### Test cases (loud failure)

Pass:
- [x] A file-origin heal whose PR opens: `succeeded`, one `healed` entry with the PR URL, no failure.
- [x] An api-origin container heal: `succeeded`; the stored config is patched.
- [x] Heal job: every target succeeds → exit 0; no target at all → exit 0 and no heal attempted.
- [x] Heal job: a source whose latest run is `ok` and at or above `min_items` is not a target, even
      with an older `error` run.
- [x] Heal job: a source whose latest run is `error` is a target; one whose latest `ok` run
      underflowed is a target.

Failure:
- [x] The LLM raises (OpenRouter 404) → failure `LLM error:` naming the target; ERROR logged.
- [x] The LLM proposes `null` → failure `no selector proposed`.
- [x] The proposal extracts nothing → failure `selector rejected:`.
- [x] The proposal does not compile (CSS `div[`, XPath `//div[`) → failure `selector rejected:` saying
      it is not a valid selector, a heal row with `applied` false, and `heal_source` returns: the
      selector is model output, so a malformed one is a rejected proposal, never a crash (2026-10-26).
- [x] File-origin, the PR call returns nothing → failure `PR not opened`; not counted as healed.
- [x] Live page extracts items with every field populated → failure `nothing to heal:`.
- [x] Unknown source → failure `source not found`; fetch error → failure `fetch failed:`.
- [x] A `HealSummary` with neither `healed` nor `failures` cannot be built.
- [x] Heal job: one target fails and another heals → exit 1, ERROR names the failed source.
- [x] Heal job, manual dispatch: `healed: []` → exit 1; a healed source → exit 0.
- [x] Queue task: a failed summary is logged at ERROR and returned, not raised.
- [x] GitHub blob creation answers 403 → ERROR log names the step and the status, never the token.
