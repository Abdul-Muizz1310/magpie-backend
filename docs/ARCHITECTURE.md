# Architecture

```mermaid
graph TD
  YAML[configs/*.yaml] --> Loader[Config loader — Pydantic validation]
  Loader --> Factory{render: js?}
  Factory -- no --> Scrapy[Scrapy spider]
  Factory -- yes --> Playwright[Playwright worker]
  Scrapy --> Pipeline[Item pipeline — hash + dedupe]
  Playwright --> Pipeline
  Pipeline --> DB[(Neon scrape branch)]
  Pipeline --> R2[R2 artifact archive]
  Pipeline --> Check{items > 0?}
  Check -- no --> Healer[Healer — LLM + GitHub PR]
  DB --> UI[FastAPI viewer API]
```

## Components

| Component | Responsibility |
|---|---|
| Config loader | Validates YAML against Pydantic schema |
| Factory | Dispatches to Scrapy or Playwright based on `render` flag |
| Item pipeline | Hashes items, deduplicates against DB, persists |
| Archive | Stores raw HTML snapshots in R2 for healer |
| Healer | Detects broken selectors, asks LLM for fix, opens PR |
| Viewer API | FastAPI endpoints: `/sources`, `/runs`, `/heals` |
