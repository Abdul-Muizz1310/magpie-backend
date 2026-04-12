<p align="center">
  <img src="assets/demo.gif" alt="demo" width="720"/>
</p>

<h1 align="center">magpie</h1>
<p align="center">
  <em>YAML-defined scrapers that self-heal via LLM + PR</em>
</p>

<p align="center">
  <a href="https://magpie-backend-izzu.onrender.com">Live Demo</a> •
  <a href="WHY.md">Why</a> •
  <a href="docs/ARCHITECTURE.md">Architecture</a> •
  <a href="docs/DEMO.md">Demo Script</a>
</p>

<p align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/Abdul-Muizz1310/magpie-backend/ci.yml" alt="ci"/>
  <img src="https://img.shields.io/github/license/Abdul-Muizz1310/magpie-backend" alt="license"/>
</p>

---

## What it does

Define a scraper in 20 lines of YAML. When a selector breaks, an LLM patches it and opens a PR. You review, you merge, you move on. Backs the CV claim about ingesting 30+ exchange rulebooks via config not code.

## The unique angle

- **One YAML = one spider** — factory pattern emits Scrapy (static) or Playwright (JS-rendered) from the same config schema
- **Self-healing via LLM + PR** — zero items triggers a healer that re-derives selectors from raw HTML and opens a GitHub PR labeled `scrape:self-heal`
- **Content-addressed deduplication** — items are hashed; nightly runs produce diffs, not full dumps
- **No auto-merge** — healer PRs require human review, keeping the audit trail readable

## Quick start

```bash
git clone https://github.com/Abdul-Muizz1310/magpie-backend.git
cd magpie-backend
cp .env.example .env
uv sync
uv run scrape run hackernews
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tech stack

| Concern | Choice |
|---|---|
| Config validation | Pydantic v2 |
| Static scraping | Scrapy |
| JS scraping | Playwright (Python) |
| Scheduling | GitHub Actions cron |
| Storage | Neon Postgres |
| Artifact storage | Cloudflare R2 |
| Healer LLM | OpenRouter |
| GitHub PRs | ghapi |
| Viewer API | FastAPI |

## Deployment

Viewer API on Render (free tier). Scheduled scrapes and heal-on-failure via GitHub Actions cron. Raw HTML snapshots archived to Cloudflare R2.

## License

MIT
