# Spec 10 — Platform token contract v2 (magpie's copy)

## Goal

magpie verifies callers exactly as the workspace's `docs/specs/platform-contract-v2.md` says (decision
DR-0033, which supersedes DR-0009 and extends DR-0008/DR-0030). This file is magpie's conformance
sheet: what the contract fixes, the values magpie plugs in, and the tests that prove it. It replaces
the verification rules of spec 09; spec 09's history (frontend key, clock skew, forwarded addresses)
still explains why.

## Values

| | |
|---|---|
| own service id (`aud`) | `"magpie"` — a constant in `platform/platform_token.py`, not configuration |
| bastion key | `BASTION_SIGNING_KEY_PUBLIC` (base64 SPKI DER), else `BASTION_PUBLIC_KEY_URL` (JSON `publicKey`, cached 1 h) |
| bastion `iss` | `"bastion"` |
| frontend key | `MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC` (base64 SPKI DER) |
| frontend `iss` / `role` | `"magpie-frontend"` / `"frontend"` |
| leeway | 10 s on `exp` and `iat` |
| max lifetime | `exp - iat <= 300` (and `> 0`) |
| test settings | `APP_ENV=test` (set by `tests/conftest.py`; production sets `APP_ENV=production`) |
| public routes | `/health`, `/version`, `/metrics`, `/openapi.json`, `/docs` and `/docs/…`, `/redoc` and `/redoc/…`. magpie has no verification surface, so nothing else is public. |

Key values are read with a leading byte-order mark and edge whitespace stripped (spec 09). A value
that is not an Ed25519 SPKI key is logged once by variable name (never the value) and treated as
absent.

## Verification

The token is read from `X-Platform-Token`, else from `Authorization: Bearer <token>`.

1. **Public route** → pass, no token needed.
2. **No trusted key** (neither key configured or parseable) → `503 {"detail":
   "platform_auth_unconfigured"}`. Under test settings the request passes instead (router tests run
   without keys). `DEMO_MODE` does not change this: demo mode tolerates a missing token, not a
   missing key.
3. **No token** → `401 {"detail": "missing_platform_token"}`; with `DEMO_MODE=true` the request
   proceeds anonymous and a WARNING is logged.
4. **Token present** — authenticated iff all of:
   1. it parses as a compact JWS whose header `alg` is `EdDSA` (`none`, `HS256`, … rejected);
   2. its signature verifies against one trusted key, and **that key decides the issuer**: bastion's
      key requires `iss == "bastion"`; the frontend key requires `iss == "magpie-frontend"` and
      `role == "frontend"`. A frontend-signed token claiming `bastion`, or the reverse, is rejected;
   3. `aud` is the string `"magpie"` (missing, another service, or a list → rejected);
   4. `exp` and `iat` are integers, `exp + 10 > now`, `iat - 10 <= now`, `0 < exp - iat <= 300`;
   5. `sub` is a non-empty string (the rate-limit key needs it).

   Otherwise `401 {"detail": "invalid_platform_token"}` and a WARNING naming the failed check
   (`malformed`, `alg`, `signature`, `iss`, `role`, `aud`, `exp`, `iat`, `lifetime`, `sub`), never
   the token. `DEMO_MODE=true` does **not** rescue an invalid token.
5. `DEMO_MODE` defaults to `false`; only `1`/`true`/`yes`/`on` (any case) enable it.

An authenticated request carries its principal (`iss`, `sub`, `role`) on `request.state`.

## Rate limiting

The limiter runs inside the token middleware. The bucket depends on the key that verified the token
(contract §2, amended 2026-10-20):

| request | bucket |
|---|---|
| bastion's key | `"bastion:{sub}"` (`sub` is the visitor pseudonym) |
| magpie-frontend's key | `"magpie-frontend:{client address}"` — the frontend's `sub` is constant, so keying on it would put every visitor of the site in one bucket |
| unauthenticated | `"{client address}"` |

The client address is what uvicorn derives from the leftmost `X-Forwarded-For` entry
(`--proxy-headers --forwarded-allow-ips '*'`, spec 09). magpie-frontend's proxy sets exactly one
address there itself.

## CORS

CORS is the outermost middleware, so a browser preflight is answered before the token check and a
401/503 still carries `Access-Control-Allow-Origin`. Allowed request headers: `content-type`,
`x-platform-token`, `x-request-id`, `authorization`.

## Conformance tests (workspace contract §3) — `tests/unit/test_platform_contract_v2.py`

1. [x] Valid bastion token, `aud` = `magpie` ⇒ authenticated (200).
2. [x] `aud` = another service's id ⇒ 401 invalid.
3. [x] `aud` missing ⇒ 401 invalid; `aud` as a list (even `["magpie"]`) ⇒ 401 invalid.
4. [x] `iss` = `bastion` but signed with the frontend key ⇒ 401 invalid (and the reverse).
5. [x] Frontend-key token with `role != "frontend"` ⇒ 401 invalid.
6. [x] Expired 5 s ago ⇒ 200; expired 15 s ago ⇒ 401 invalid.
7. [x] `iat` 5 s in the future ⇒ 200; 15 s in the future ⇒ 401 invalid.
8. [x] `alg` = `none` or `HS256` ⇒ 401 invalid.
9. [x] Tampered payload ⇒ 401 invalid.
10. [x] No token, `DEMO_MODE=false` ⇒ 401 missing; no token, `DEMO_MODE=true` ⇒ 200, WARNING logged.
11. [x] Present-but-invalid token with `DEMO_MODE=true` ⇒ still 401 invalid.
12. [x] No trusted key (non-test settings) ⇒ 503 on a protected route, 200 on `/health`.
13. [x] Every public route answers without a token (on the real app, keys configured).
14. [x] Rate-limit key is `bastion:{sub}` for a bastion token and `magpie-frontend:{forwarded address}`
    for a frontend token; two visitors through the frontend land in different buckets.

magpie extras:

- [x] Valid frontend token (`iss magpie-frontend`, `aud magpie`, `role frontend`) ⇒ 200.
- [x] `exp - iat` = 300 ⇒ 200; 301 ⇒ 401; `exp <= iat` ⇒ 401; non-integer `exp`/`iat` ⇒ 401.
- [x] Missing `iss`, missing `sub` ⇒ 401 invalid.
- [x] `Authorization: Bearer` is read when `X-Platform-Token` is absent; the header wins when both.
- [x] The WARNING for an invalid token names the check and never contains the token.
- [x] `DEMO_MODE` parsing: unset ⇒ false; `true`/`1`/`yes`/`on` ⇒ true; `false`/`0`/junk ⇒ false.
- [x] Under test settings with no key, a protected route passes.
- [x] A path that merely starts with `/docs` (e.g. `/docsx`) is not public.
- [x] Composed app: a preflight to a protected route with `Access-Control-Request-Headers:
      x-platform-token` is answered 200 and allows the header; a 401 carries
      `Access-Control-Allow-Origin` for an allowed origin.
- [x] Composed app: two bastion visitors (different `sub`) from one address get separate buckets;
      two frontend visitors (one token shape, different forwarded addresses) get separate buckets and
      one visitor's repeat calls share one; unauthenticated callers fall back to the forwarded
      address.
- [x] A malformed key value warns once by variable name and counts as absent (with no other key ⇒
      503 outside test settings).
