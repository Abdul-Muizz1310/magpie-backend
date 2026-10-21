# Spec 11 — Per-request query observability (slowquery-detective 0.2)

## Goal

bastion's dossier "measure" stage tags the calls it makes with `X-Request-Id: R` and then asks every
service `GET /_slowquery/queries?request_id=R` which queries R caused. magpie answers that with
slowquery-detective 0.2 (workspace plan WP-8, interface I-2; library spec
`slowquery-detective/docs/specs/07-fleet-observability.md`).

## Wiring (`platform/query_observability.py`, called from `magpie.main.create_app`)

- `install(app, engine, service="magpie", store_url=…, authorize=authorize_bastion)` runs **after**
  magpie's own middleware (rate limit, platform token, request id, CORS), so the library's request
  tagger is the outermost layer and every statement the request causes is attributed to it.
- `engine` is the app's own engine (`storage.db.get_engine()`, or the one `create_app` is given). It
  must exist at install time; creating it opens no connection.
- `store_url_for(DATABASE_URL)`: the URL itself when it is a Postgres URL (`postgres://`,
  `postgresql://`, `postgresql+asyncpg://`, `postgresql+psycopg://`), so data lands in schema
  `slowquery` of magpie's own database; `None` otherwise (SQLite in tests ⇒ the in-memory store).
- `authorize_bastion(request)`: `True` only when the platform-token middleware authenticated the
  request with **bastion's key** (principal minter `bastion`, `iss == "bastion"`). `/_slowquery` is
  not a public route, so the token middleware runs first: no token ⇒ its 401, an invalid token ⇒ its
  401; a valid **frontend** token reaches the hook and gets 403; anonymous `DEMO_MODE` or test-settings
  requests carry no principal ⇒ 403.
- Logging: the library logs under `slowquery.*`; an in-process Alembic run must not disable those
  loggers (the `fileConfig(disable_existing_loggers=False)` fix in `alembic/env.py`).

## Test cases

Pass:
- [x] Integration (real Postgres, Postgres store): `POST /api/scrape/{source}/once` with a bastion token
      and `X-Request-Id: R` ⇒ `GET /_slowquery/queries?request_id=R` with a bastion token answers 200
      and lists the statements the scrape ran (the `runs` and `items` writes among them), each with
      `call_count >= 1` and null percentiles; the rows live in schema `slowquery` of that database.
- [x] Integration: a request without `X-Request-Id` adds nothing to R; an unknown request id ⇒ `[]`.
- [x] Unit: `store_url_for` returns Postgres URLs as given and `None` for SQLite, blank and unset.
- [x] Unit: `authorize_bastion` is `True` for a bastion principal only.

Failure:
- [x] `GET /_slowquery/queries?request_id=R` without a token ⇒ 401 `missing_platform_token`.
- [x] With an invalid token ⇒ 401 `invalid_platform_token`.
- [x] With a valid magpie-frontend token ⇒ 403.
- [x] `DEMO_MODE` with no token, and test settings with no key ⇒ 403 (no principal).
- [x] After an in-process `alembic upgrade head`, the `slowquery.*` loggers are still enabled.
