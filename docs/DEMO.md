# Demo Script

Step-by-step demo for interviews. Total runtime: ~60 seconds.

## Prerequisites

```bash
git clone https://github.com/Abdul-Muizz1310/magpie-backend.git
cd magpie-backend
cp .env.example .env   # fill in OpenRouter key at minimum
uv sync
```

## 1. Show the config (10s)

Open `configs/hackernews.yaml`. Point out:
- 20 lines of YAML defines a complete scraper
- `render: false` means Scrapy (static HTML)
- `item.fields` lists CSS selectors
- `health.min_items: 20` is the healer threshold

## 2. Run a scrape (10s)

```bash
uv run python -c "
from magpie.config.loader import load_config_from_file
from magpie.scrapy.factory import run_spider
from pathlib import Path

config = load_config_from_file(Path('configs/hackernews.yaml'))
# Override to single page for demo speed
config = config.model_copy(update={'pagination': {'max_pages': 1}})
items = run_spider(config)
print(f'Collected {len(items)} items')
for item in items[:3]:
    print(f'  {item[\"id\"]}: {item[\"title\"]}')
"
```

Expected output: 30 items with titles and IDs.

## 3. Show deduplication (10s)

```bash
uv run python -c "
from magpie.storage.repo import ItemRepository

repo = ItemRepository()
items = [{'title': 'Article 1', 'id': '1'}, {'title': 'Article 2', 'id': '2'}]

r1 = repo.persist_items('demo', items, dedupe_key='id')
print(f'Run 1: {r1.items_new} new, {r1.items_updated} updated, {r1.items_removed} removed')

r2 = repo.persist_items('demo', items, dedupe_key='id')
print(f'Run 2: {r2.items_new} new, {r2.items_updated} updated, {r2.items_removed} removed')

items[0]['title'] = 'Updated Title'
r3 = repo.persist_items('demo', items, dedupe_key='id')
print(f'Run 3: {r3.items_new} new, {r3.items_updated} updated, {r3.items_removed} removed')
"
```

Expected: Run 1 shows 2 new. Run 2 shows 0 changes. Run 3 shows 1 updated.

## 4. Show the healer logic (10s)

```bash
uv run python -c "
from magpie.healer.detector import should_heal
from magpie.healer.validator import validate_selector
from pathlib import Path

# Healer triggers when items < threshold
print(f'0 items, min 20: heal={should_heal(item_count=0, min_items=20)}')
print(f'30 items, min 20: heal={should_heal(item_count=30, min_items=20)}')

# Validator checks selectors against HTML
html = Path('fixtures/hackernews-v1.html').read_text()
good = validate_selector(html, 'span.titleline > a')
bad = validate_selector(html, 'span.nonexistent')
print(f'Good selector: {len(good)} matches')
print(f'Bad selector: {len(bad)} matches')
"
```

## 5. Show the broken config (10s)

Open `configs/demo-broken.yaml`. Point out `span.nonexistent-class` — deliberately wrong.

"When this runs and returns 0 items, the healer fires: it sends the raw HTML to an LLM, gets a proposed fix, validates it, and opens a GitHub PR. No auto-merge. You review the diff and approve."

## 6. Show the viewer API (10s)

Open browser to `https://magpie-backend-izzu.onrender.com/health`

Then show `/sources`, `/runs`, `/heals` endpoints.

"The frontend (magpie-frontend) consumes these endpoints to show a dashboard with run history and diff views."
