# Spec: Content-Addressed Deduplication

## Goal

Hash scraped items deterministically so that repeated scrapes of the same content produce no duplicates. Each run produces a diff: new items, updated items (same dedupe_key but different content hash), and removed items (present in DB but absent from current scrape). This turns nightly scrapes from "full dumps" into meaningful change feeds.

## Inputs

- A list of scraped item dicts (from factory output)
- The `SourceConfig` for the current source (provides `item.dedupe_key` and `item.fields`)

## Outputs

- Per-item: a content hash (SHA-256 of normalized item data)
- Per-run: counts of `items_new`, `items_updated`, `items_removed`
- DB state: `items` table updated with current hashes and timestamps

## Invariants

- Hash is deterministic: same item data always produces the same hash
- Hash is normalized: leading/trailing whitespace stripped, keys sorted, unicode NFC-normalized
- Two items with different field values produce different hashes
- Two items with same values but different whitespace produce the same hash
- `dedupe_key` is the stable identity — if an item's dedupe_key exists in DB but hash changed, it is an update (not new + removed)
- Items in DB but not in current scrape are marked `removed_at = now()` (soft delete)
- Items previously removed that reappear get `removed_at = null` and `seen_last` updated
- `seen_first` is never modified after initial insert
- `seen_last` is updated on every scrape where the item is present

## Data model

```sql
items (
  source      text references sources(name),
  dedupe_key  text not null,
  hash        text not null,
  data        jsonb not null,
  seen_first  timestamptz default now(),
  seen_last   timestamptz default now(),
  removed_at  timestamptz,
  primary key (source, dedupe_key)
)
```

## Test cases

### Happy path
- [ ] First scrape: all items inserted as new, items_new = N, items_updated = 0, items_removed = 0
- [ ] Second scrape with identical items: items_new = 0, items_updated = 0, items_removed = 0, seen_last updated
- [ ] Second scrape with one item changed: items_updated = 1 (hash changed), data updated in DB
- [ ] Second scrape with one item missing: items_removed = 1, removed_at set on missing item
- [ ] Second scrape with one new item added: items_new = 1

### Edge cases
- [ ] Item with extra whitespace in values hashes same as trimmed version
- [ ] Item with unicode combining characters normalized to NFC before hashing
- [ ] Previously removed item reappears: removed_at cleared, seen_last updated, counts as new
- [ ] Empty scrape (0 items): all existing items marked removed
- [ ] Item with dedupe_key containing special characters (hyphens, dots) handled correctly
- [ ] Very large item data (>10KB JSON) hashes correctly

### Failure cases
- [ ] Item missing the dedupe_key field raises clear error (not silent skip)
- [ ] Duplicate dedupe_keys within a single scrape raises error
- [ ] DB connection failure during persist raises, does not silently lose data

## Acceptance criteria

- [ ] All test cases pass
- [ ] Hashing is pure (no side effects, unit-testable without DB)
- [ ] Persist logic uses DB transactions (all-or-nothing per run)
- [ ] Run summary (new/updated/removed counts) stored in `runs` table
- [ ] Integration tests use Testcontainers postgres, not mocks
