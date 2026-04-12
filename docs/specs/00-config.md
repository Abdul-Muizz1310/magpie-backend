# Spec: Config Loader + Schema

## Goal

Load YAML files from `configs/` and validate them into typed Pydantic models. A single `SourceConfig` model captures everything needed to run a scraper: URL, selectors, pagination, JS-render options, health thresholds, and scheduling. Invalid configs fail fast at load time with actionable error messages.

## Inputs

- A YAML file path or a directory of YAML files (`configs/*.yaml`)

## Outputs

- A validated `SourceConfig` Pydantic model (or list of models from the registry)
- A `ValidationError` with field-level detail on invalid input

## Invariants

- `name` must match `^[a-z0-9-]+$` (lowercase alphanumeric + hyphens only)
- `url` must be a valid HTTP(S) URL
- `render: true` configs may have `wait_for` and `actions`; `render: false` configs must not
- `item.fields` must be non-empty
- `item.dedupe_key` must reference a field name that exists in `item.fields`
- `schedule` must be a valid cron expression
- `health.min_items` must be >= 0
- `health.max_staleness` must parse as a duration string (e.g. "24h", "6h", "30m")
- `pagination.max_pages` must be >= 1
- `actions[].type` must be one of: click, wait, scroll, type
- `actions` with type `click` or `type` require `selector`
- `actions` with type `wait` require `ms`

## Models

```python
SourceConfig
  name: str (pattern ^[a-z0-9-]+$)
  description: str = ""
  url: HttpUrl
  render: bool = False
  schedule: str (cron)
  rate_limit: RateLimitDef = {rps: 1}
  item: ItemDef
  pagination: PaginationDef = PaginationDef()
  wait_for: str | None = None
  actions: list[ActionDef] = []
  health: HealthDef = HealthDef()

ItemDef
  container: str
  fields: list[FieldDef] (min_length=1)
  dedupe_key: str (must match a field name)

FieldDef
  name: str
  selector: str
  attr: str | None = None

PaginationDef
  next: str | None = None
  max_pages: int = 1 (ge=1)

ActionDef
  type: Literal["click", "wait", "scroll", "type"]
  selector: str | None = None
  ms: int | None = None
  text: str | None = None

HealthDef
  min_items: int = 1 (ge=0)
  max_staleness: str = "24h"

RateLimitDef
  rps: int = 1 (ge=1)
```

## Test cases

### Happy path
- [ ] Valid static config (hackernews-like) parses to SourceConfig with correct field values
- [ ] Valid JS-render config with wait_for + actions parses correctly
- [ ] Config with all optional fields omitted uses defaults (pagination.max_pages=1, health.min_items=1, render=false)
- [ ] Registry loads all 4 shipped configs from `configs/` directory without error
- [ ] Config with pagination.next=null and max_pages=1 is valid (no pagination)

### Edge cases
- [ ] Config with description containing unicode characters parses correctly
- [ ] Config with rate_limit.rps=10 parses correctly
- [ ] Minimal valid config (only required fields) parses

### Failure cases
- [ ] Name with uppercase letters rejected
- [ ] Name with spaces rejected
- [ ] Name with underscores rejected
- [ ] Empty name rejected
- [ ] Invalid URL (missing scheme) rejected
- [ ] Empty item.fields list rejected
- [ ] dedupe_key referencing non-existent field name rejected
- [ ] render=false with actions list non-empty rejected
- [ ] render=false with wait_for set rejected
- [ ] action type=click without selector rejected
- [ ] action type=wait without ms rejected
- [ ] action type=type without selector rejected
- [ ] pagination.max_pages=0 rejected
- [ ] health.min_items=-1 rejected
- [ ] Malformed YAML (syntax error) raises clear error
- [ ] YAML with unknown top-level keys rejected (strict mode)
- [ ] Duplicate field names in item.fields rejected

## Acceptance criteria

- [ ] All test cases pass
- [ ] 4 shipped YAML configs validate without error
- [ ] ValidationError messages include the field path and constraint violated
- [ ] Registry discovers all .yaml files in configs/ directory
- [ ] No untyped dicts cross module boundaries — everything is a Pydantic model
