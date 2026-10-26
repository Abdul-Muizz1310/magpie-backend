# Spec 12 — Langfuse tracing of the self-heal agent (Langfuse v4, Python SDK v5)

## Goal

Every heal is one Langfuse trace that reads like the heal ran: which page was fetched, which
selectors were broken, what the model proposed on each attempt (and what it cost in tokens), whether
the proposal held against the live page, and which PR or config patch it produced — or exactly
where it stopped. The target is the Langfuse v4 platform (observations-first; Langfuse Cloud is
v4-only from 2026-11-16) through **Python SDK v5** (`langfuse>=5.0.4,<6`), the SDK Langfuse names
for Cloud.

Until now nothing in magpie was traced: the `LANGFUSE_*` lines in `.env.example` named an
integration no code read. The heal's only record was its log lines and the `heals` rows, which keep
the proposal but not the prompt, the failed attempts, the model's reply or the token spend.

## Client (`core/tracing.py`)

- Built once, on first use, from the environment: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
  `LANGFUSE_BASE_URL` (US Cloud: `https://us.cloud.langfuse.com`; unset, the SDK would send to the
  EU region). **Both keys** set → tracing is on. Either blank or unset → the client is built with
  `tracing_enabled=False`: every observation is a no-op, nothing is exported or sent.
- **Environment** = `APP_ENV` (`production` on Render and in the heal job, `development` locally,
  `test` in the suite; unset → `development`). `APP_ENV` must be a valid Langfuse environment:
  1-40 of `a-z`, `0-9`, `_`, `-`, not starting with `langfuse`. Anything else raises `ValueError`
  when the client is built — and the client is built **at startup** (the FastAPI lifespan, the heal
  CLI's `main`), so a bad value stops the boot instead of a heal.
- **Release** is left to the SDK, which reads `RENDER_GIT_COMMIT` on Render and `GITHUB_SHA` in the
  heal job.
- **Export** runs in the SDK's background batches; nothing on the heal path flushes.
  `shutdown()` (flush + stop) runs when the app stops (the lifespan that also owns the embedded
  Procrastinate worker) and at the end of `python -m magpie.healer.run`, a short-lived GitHub
  Actions process that would otherwise exit with spans still buffered.
- Instrumentation is manual: the healer calls OpenRouter over raw `httpx`, and no framework
  integration would see those calls.

## Trace shape

One trace per `heal_source` call. Names are stable and verb-first (evaluators, dashboards and saved
filters target them); run-specific values live in metadata, never in a name.

| Step | Type | Name | Input | Output |
|---|---|---|---|---|
| the heal | `agent` | `heal-source` | `{source, run_id}` | the `HealSummary` as JSON: `{source, origin, healed, failures}` |
| live page fetch | `tool` | `fetch-page` | `{url, render}` | `{char_count}` |
| one broken target (container or field) | `agent` | `heal-selector` | `{target, old_selector, selector_type, mode}` | healed: `{healed: true, new_selector, mode, pr_url, applied}`; not: `{healed: false, reason}` |
| each model attempt | `generation` | `propose-selector` | the chat messages | `{role: "assistant", content[, reasoning]}` |
| proposal check | `evaluator` | `validate-selector` | `{selector, selector_type}` | `{accepted, match_count, sample_values}` (the first three matches, each cut to 200 characters) |
| file-origin fix | `tool` | `open-heal-pr` | `{source, target, file_path, new_selector}` | `{pr_url}` |
| api-origin fix | `tool` | `patch-source-config` | `{source, target, new_selector}` | `{applied: true}` |

`validate-selector` is an **evaluator**, not a guardrail: it scores the correctness of the model's
output against the live page (does the proposed selector match anything), which is Langfuse's
definition of an evaluator; a guardrail screens content for harm or jailbreaks, which this does
not. The rejection itself is its output (`accepted: false`), not an error of the check.

Nesting: `fetch-page` and every `heal-selector` sit under `heal-source`; a target's generations,
its check and its PR or patch sit under its `heal-selector`.

### Generations

`healer/selector_fixer._call_llm` makes one HTTP attempt; each is its own generation (never one
generation wrapping the retries):

- `model`: the slug asked (`OPENROUTER_MODEL_PRIMARY`, else `DEFAULT_MODEL`).
- `model_parameters`: `{temperature: 0.1, response_format: "json_object"}`.
- `metadata`: `attempt` (1-based, of `MAX_RETRIES`), `provider` (OpenRouter's upstream provider,
  when reported).
- `usage_details`, from OpenRouter's `usage`, as **exclusive** buckets (Langfuse adds buckets up):
  `input = prompt_tokens − cached_tokens`, `input_cached_tokens` (when > 0),
  `output = completion_tokens − reasoning_tokens`, `output_reasoning_tokens` (when > 0). A missing
  or unreadable count drops its bucket; usage never fails the call.
- `cost_details = {"total": usage.cost}` when OpenRouter reports a number (`0` on `:free` models).
- `output`: the assistant message, with OpenRouter's `reasoning` text when the model returned one.
- A failed attempt is level `ERROR` with `status_message` naming the stage: `rate_limited:
  status=429`, `server_error: status=5xx`, `client_error: status=4xx`, `timeout`,
  `http_error: <type>`, or `bad_response: <type>` (a reply that is not the JSON proposal). The
  exception still reaches the retry loop unchanged. A cancelled attempt is `ERROR` too
  (`cancelled before the model answered`).
- OpenRouter can answer **200 with its error object** instead of a completion (seen live on
  2026-10-26: `rate_limit_exceeded` from the upstream provider). That attempt is filed under the
  error's own code — `rate_limited: status=429 in the reply body` (`upstream_error: in the reply
  body` without a code) — and the error object (`code`, `message`) is the generation's output, so
  the cause is on the trace rather than a schema complaint.

The raw-HTML prompt is public page content (the source's own URL), so inputs and outputs are kept
in full.

### Trace attributes

Set with `propagate_attributes` on entering `heal-source`, so every observation carries them:

- `trace_name`: `heal-source`.
- `tags`: `["magpie", "trigger:<worker|cli>"]` — the service (all products share one Langfuse
  project) and what started the heal: the Procrastinate `heal_source_task` in the web service, or
  `python -m magpie.healer.run` in GitHub Actions. `heal_source` takes the trigger explicitly.
- `metadata`: `{"source": <name>, "run_id": <uuid>}` (`run_id` only when the heal has one).
- **No session.** No request id reaches a heal: the worker runs the deferred job outside any
  request (its arguments are the source and the run id), and the CLI has no request at all. A
  dossier's scrape and the heal it later triggers are therefore separate traces.

## Failure behaviour

- `heal-source` is `ERROR` whenever the summary is not `succeeded`, with `status_message` =
  `HealSummary.describe_failures()`; `DEFAULT` when every target healed.
- `heal-selector` is `ERROR` when its target failed, with the failure's reason (`LLM error: …`,
  `no selector proposed…`, `selector rejected: …`, `PR not opened…`).
- `fetch-page` is `ERROR` when the re-fetch raised (the SDK records the exception); the heal still
  ends with its `fetch failed:` summary.
- `open-heal-pr` is `ERROR` (`PR not opened`) when `create_heal_pr` returns nothing.
- Tracing never changes a heal: an exporter that fails leaves the summary, the `heals` rows, the
  patched config and the CLI's exit code as they would be without tracing.

## Test cases

Fast tier (`tests/unit/test_tracing.py`, a client on an in-memory exporter — no network):

- **T1** both keys → an enabled client with the configured base URL and `APP_ENV` as environment
  (unset → `development`); either key blank or unset → tracing disabled; the client is built once.
- **T2** `APP_ENV`: `production`, `test`, `dev_1`, `pr-12` accepted; `Production`, `langfuse-ci`,
  `""`, a 41-character value and `prod env` rejected with `ValueError` when the client is built.
- **T3** `usage_details`: `{}` with no counts; `{input, output}`; cached and reasoning tokens split
  out; details larger than their total clamped; a detail without its total dropped.
- **T4** `cost_details`: a number → `{total}`; `None` → `None`.
- **T5** one answered `_call_llm` → one `propose-selector` generation with the model, parameters,
  the prompt as a user message, the assistant reply, exclusive usage, cost, `attempt` and
  `provider`; unreadable usage (`"10"`, `-3`, `true`, `NaN`, a string, a list) never fails the
  call and records no usage.
- **T6** a 429 then a valid reply through `fix_selector` → two generations: attempt 1 `ERROR`
  `rate_limited: status=429`, attempt 2 `DEFAULT`; a 500 → `server_error: status=500`; a reply
  that is not the proposal → `bad_response: ValidationError`; three failures → three `ERROR`
  generations and the last error raised. A 200 carrying OpenRouter's error object with code 429 →
  `rate_limited: status=429 in the reply body` and the error as the output; with code 502 →
  `server_error: status=502 in the reply body`; with no code → `upstream_error: in the reply
  body`.
- **T7** a composed api-origin container heal (`heal_source`, HTTP stubbed): one trace; root
  `heal-source` (`agent`) with `{source, run_id}` in and the summary out; children `fetch-page`
  then `heal-selector`; under it `propose-selector`, `validate-selector` (`accepted: true`) and
  `patch-source-config`; every observation has trace name `heal-source`, tags `magpie` and
  `trigger:worker`, environment `test`, `source` and `run_id` metadata, and no session.
- **T8** a file-origin field heal whose PR opens → `open-heal-pr` with the PR URL; the trace is
  `DEFAULT` throughout.
- **T9** a proposal that matches nothing → `validate-selector` output `accepted: false`;
  `heal-selector` and `heal-source` `ERROR` with `selector rejected: …`.
- **T10** the model failing every attempt → `heal-selector` and `heal-source` `ERROR` with
  `LLM error: …`; three `ERROR` generations beneath.
- **T11** file-origin, the PR call returns nothing → `open-heal-pr` and `heal-selector` `ERROR`
  `PR not opened…`.
- **T12** a failed re-fetch → `fetch-page` `ERROR`, root `ERROR` with `fetch failed: …`; an unknown
  source → a root with no children, `ERROR` `source: source not found`.
- **T13** an exporter that raises on every export → the heal's summary and stored rows unchanged.
- **T14** the heal job: `main()` shuts tracing down once, also when the heal raises; its heals are
  tagged `trigger:cli`; the queue task passes `trigger="worker"`.
- **T15** the app lifespan builds the client at startup (an invalid `APP_ENV` stops it) and shuts
  tracing down once on exit.

Committed config (`tests/unit/test_ops_baseline.py`):

- **T16** `heal-on-failure.yml`'s heal step passes `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
  from repo secrets, `APP_ENV: production`, and the same `LANGFUSE_BASE_URL` as `render.yaml`, which
  declares both keys as `sync: false`.

## Acceptance

- `langfuse>=5.0.4,<6`, type-checked by mypy strict against the SDK's own types.
- Every case above is a test; coverage of `core/tracing.py` is 100%.
- No per-span `flush()`; nothing is logged or traced that carries a secret.
