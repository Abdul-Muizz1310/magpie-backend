# Spec: Spider Factory

## Goal

Given a validated `SourceConfig`, produce the correct scraper: a Scrapy spider class for `render: false` configs, or a Playwright-based runner for `render: true` configs. The factory is the single dispatch point — callers never need to know which engine runs underneath.

## Inputs

- A validated `SourceConfig` model

## Outputs

- For `render: false`: a dynamically-created Scrapy `Spider` subclass configured with the source's URL, selectors, pagination, and rate limit
- For `render: true`: a `PlaywrightRunner` instance configured with the source's URL, wait_for, actions, selectors, and rate limit

## Invariants

- Factory never returns the wrong engine type for the config's `render` flag
- Scrapy spider respects `ROBOTSTXT_OBEY = True` by default
- Scrapy spider uses `AsyncioSelectorReactor` to avoid Twisted/asyncio conflicts
- Playwright runner always runs headless (controlled by `PLAYWRIGHT_HEADLESS` env var)
- Both engines extract items matching the `item.container` selector
- Both engines extract field values using `item.fields[].selector`
- Both engines respect `rate_limit.rps`
- Scrapy spider follows pagination via `pagination.next` selector up to `pagination.max_pages`
- Playwright runner executes `actions` in order before extracting items
- Both engines archive raw HTML to R2 before parsing (for healer)
- Both engines return a list of dicts keyed by field name

## Test cases

### Happy path
- [ ] `render: false` config produces a Scrapy Spider subclass
- [ ] `render: true` config produces a PlaywrightRunner instance
- [ ] Scrapy spider extracts items from local fixture HTML (hackernews fixture, expect >= 5 items)
- [ ] Playwright runner extracts items from local fixture HTML served by a test server
- [ ] Scrapy spider follows pagination link and scrapes page 2
- [ ] Scrapy spider stops at max_pages limit
- [ ] Playwright runner executes click action before extraction
- [ ] Playwright runner waits for wait_for selector before extraction

### Edge cases
- [ ] Config with no pagination (next=null) — spider scrapes only one page
- [ ] Config with empty actions list — Playwright runner skips action phase
- [ ] Field with attr set extracts attribute instead of text
- [ ] Container selector matching zero elements returns empty list (not an error at factory level)

### Failure cases
- [ ] Scrapy spider handles network timeout gracefully (logs error, does not crash)
- [ ] Playwright runner handles page load timeout gracefully
- [ ] Playwright runner handles missing wait_for selector (timeout, not hang)
- [ ] Invalid CSS selector in config raises clear error during extraction

## Acceptance criteria

- [ ] All test cases pass
- [ ] Factory dispatches correctly for all 4 shipped configs
- [ ] Scrapy integration test runs against local fixture HTTP server (no network)
- [ ] Playwright integration test runs against local fixture HTTP server (no network)
- [ ] Raw HTML archived before parsing in both engines
