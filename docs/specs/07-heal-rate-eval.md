# Spec 07 — Heal-rate eval

## Goal

Publish **one number that reproduces**: the share of selector-breakage cases in
`evals/heal_rate_cases.yaml` that magpie's heal loop actually repairs.

```bash
uv run magpie-eval                       # prints the table, exits non-zero on drift
uv run magpie-eval --json evals/heal_rate.json
```

The number is committed to `evals/heal_rate.json` and asserted by
`tests/integration/test_heal_rate_eval.py`, so it cannot silently drift away from
the README.

## Why the proposer is deterministic, not the LLM

The shipped healer asks an LLM (`healer/selector_fixer.fix_selector`) for the new
selector. An LLM-in-the-loop eval would need network and API credit on every run
and would not return the same number twice — it could not be a committed
baseline, and a "heal rate" nobody can re-derive is exactly the kind of claim
this repo should not make.

So the eval swaps only the *proposal* step for `evals.offline_proposer`, a
value-anchored re-derivation that needs no network:

1. Read the field's previously-extracted values from the **before** HTML.
2. Search the **after** HTML for elements whose own text — or any attribute
   value — equals one of those values.
3. Build CSS fragments locating those elements relative to the item container,
   least-specific first, and keep the first that matches in a majority of
   containers.
4. For a container break, score every repeated `tag.class` signature by how many
   of the config's fields it can anchor, and take the best.

Every other stage is the shipped code: `scrapy.factory._extract_items_from_html`
for extraction, `healer.detector.should_heal` for the threshold,
`healer.validator.validate_selector` for validation, `healer.apply.patched_yaml`
for writing the fix back into the config.

**What the published number therefore means:** the share of breakage archetypes
that magpie's detect → propose → validate → patch → re-extract loop repairs when
the proposal step is the offline proposer. It is *not* a measurement of the
LLM's accuracy. Value anchoring is deployable, not synthetic — production already
stores prior extracted values in `items.data`.

## Case contract

Each entry in `evals/heal_rate_cases.yaml` names a config plus a before/after
HTML pair:

```yaml
cases:
  - name: hackernews-field-class-rename
    description: <span class="titleline"> renamed to storylink
    config: configs/hackernews.yaml
    before: fixtures/hackernews-v1.html
    after: fixtures/hackernews-v2-broken.html
    expect_healable: true
```

A case is **valid** only if:

1. `before` extracts at least one item in which *every* field is non-`None`.
2. `after` is detected as broken by the shipped detector — either zero items
   (container break) or at least one field that is `None` across every item.

An invalid case raises `InvalidEvalCaseError`. A silently-not-broken "after"
fixture would inflate the heal rate, so this fails the eval rather than skipping.

A case **heals** when, after patching, re-extracting `after` yields
`>= health.min_items` items *and* leaves no field `None` across every item — i.e.
`detect_breakage` reports no breakage on the patched config.

## Test cases (enumerated before implementation)

Proposer, field level:
- [x] Class rename on the field's wrapper → re-derives via the anchor element.
- [x] Attribute renamed (`href` → `data-href`) → proposes `::attr(data-href)`.
- [x] A field read off the container itself (`::attr(id)`) → proposes an empty
      fragment plus the attribute accessor.
- [x] Extra wrapper element inserted → the `>` chain breaks, the loose fragment
      still resolves.
- [x] Values absent from the after HTML → returns `None`, no guess.
- [x] Empty / all-blank old samples → returns `None`.
- [x] Container selector matches nothing → returns `None`.
- [x] A candidate that resolves in only a minority of containers is rejected.

Proposer, container level:
- [x] Container class renamed → re-derives the repeated row signature.
- [x] Prefers the ancestor covering the most fields over the deepest repeated
      element (the row, not the title span).
- [x] A signature occurring once is never proposed (an item container repeats).
- [x] No anchors found → returns `None`.

Driver:
- [x] `detect_breakage` distinguishes container break, field break, and healthy.
- [x] A before fixture with an all-`None` field is rejected as an invalid case.
- [x] An after fixture that is not broken is rejected as an invalid case.
- [x] Container + field breakage in one page heals in two stages.
- [x] A genuinely unhealable case is reported as a miss, not an error.
- [x] `heal_rate` is `healed / total`; empty case list raises rather than
      returning a meaningless 0/0.
- [x] Every case's outcome matches its declared `expect_healable`.
- [x] The measured rate equals the committed `evals/heal_rate.json` baseline.
