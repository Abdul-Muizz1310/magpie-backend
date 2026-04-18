# Demo Script

Step-by-step demo. Total runtime: ~90 seconds.

## Prerequisites

```bash
git clone https://github.com/Abdul-Muizz1310/magpie-backend.git
cd magpie-backend
cp .env.example .env   # fill DATABASE_URL (Neon) + OPENROUTER_API_KEY
uv sync
uv run playwright install chromium
uv run magpie migrate
uv run magpie sync
```

## 1. Show the config (10s)

Open `configs/hackernews.yaml`. Point out:
- ~20 lines of YAML defines a complete scraper
- `render: false` means httpx + parsel (static HTML path)
- `item.fields` lists CSS selectors
- `health.min_items: 20` is the healer threshold

Then open `configs/wikipedia-current-events.yaml`:
- Same schema, but `container_type: xpath` and `selector_type: xpath` per field
- Demonstrates that CSS and XPath are first-class in the same schema

## 2. Run a scrape synchronously (10s)

```bash
uv run magpie run hackernews
# ok: source=hackernews run_id=<uuid> items=30
```

Then hit the viewer:
```bash
curl -s localhost:8000/runs?source=hackernews | jq '.[0]'
```

## 3. Show deduplication (10s)

```bash
uv run magpie run hackernews   # first run — items_new=30, items_updated=0
uv run magpie run hackernews   # second run — items_new=0, items_updated=<few>
```

The counts come from content-addressed hashing (SHA-256 + NFC). Re-running doesn't produce phantom updates.

## 4. Enqueue an async scrape (10s)

```bash
# Start the API + embedded Procrastinate worker
uv run uvicorn magpie.main:app &

curl -s -X POST localhost:8000/api/scrape/hackernews/enqueue | jq
# { "run_id": "...", "job_id": "42", "source": "hackernews", "status": "queued" }

# Poll
curl -s localhost:8000/api/runs/<run_id> | jq '.status'
# "queued" → "running" → "ok"
```

## 5. Submit a custom source with a broken selector (15s)

```bash
curl -s -X POST localhost:8000/api/sources \
  -H "Content-Type: application/json" \
  -d '{"yaml": "name: broken-demo\nurl: https://news.ycombinator.com\nschedule: \"0 0 * * 0\"\nitem:\n  container: \"tr.athing\"\n  fields:\n    - {name: title, selector: \".does-not-exist::text\"}\n    - {name: id, selector: \"::attr(id)\"}\n  dedupe_key: id\nhealth:\n  min_items: 20\n"}'
```

Then enqueue a scrape. The scrape succeeds but returns items with `title=null`. The queue task notices `items < health.min_items` and defers `heal_source_task`. The healer:
1. Re-fetches the HTML (with a real User-Agent + follow_redirects).
2. Runs the same extractor the scraper does. Finds `title` is `None` across every container match.
3. Asks the LLM for a replacement selector, validates it against the HTML.
4. Because this source is `origin=api`, it **writes the fix back to `sources.config_yaml`** in Postgres directly (no PR).

```bash
curl -s localhost:8000/api/sources/broken-demo | jq .config_yaml
# Shows the healed selector
```

## 6. Show the heal history (5s)

```bash
curl -s "localhost:8000/heals?source=broken-demo" | jq
```

Each heal row records: field name, old + new selector, confidence, LLM reasoning, whether it was applied.

## 7. Show the viewer API in a browser (10s)

- `https://magpie-backend-izzu.onrender.com/sources` → source list
- `https://magpie-backend-izzu.onrender.com/runs` → run history
- `https://magpie-backend-izzu.onrender.com/heals` → heal history

The frontend (magpie-frontend) consumes these.

## 8. The weekly cron + heal-on-failure flow (optional)

- `nightly-scrape.yml` runs every Sunday 00:00 UTC, matrix strategy over every file-origin config.
- If any job fails, `heal-on-failure.yml` fires via `workflow_run` trigger, pulls the failed-run records from Postgres, and invokes the healer for each source.
- File-origin sources get a GitHub PR labeled `scrape:self-heal`; api-origin sources get the fix written straight to `sources.config_yaml`.
