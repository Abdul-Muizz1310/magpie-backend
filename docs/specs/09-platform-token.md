# Spec 09 — Platform token: bastion's key and the frontend's own key

## Goal

`DEMO_MODE=false` with bastion's public key set makes the `X-Platform-Token`
middleware (`src/magpie/platform/platform_token.py`) demand a bastion-minted
EdDSA JWT on every non-exempt route. Only bastion could mint one, so
magpie-frontend, which calls this service directly, got 401 on every data call.

The frontend now holds its own Ed25519 key, mints a 60-second token server-side
and proxies the browser's calls (DR-0030). This service trusts **bastion's key
and the frontend's key**, and accepts a frontend-key token only when it is
scoped to this service with the frontend role.

## Token contract

| | bastion token | frontend token |
|---|---|---|
| key | `BASTION_SIGNING_KEY_PUBLIC` (base64 SPKI DER) or the cached `BASTION_PUBLIC_KEY_URL` fetch | `MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC` (base64 SPKI DER), env only, no URL fetch |
| `alg` | `EdDSA` | `EdDSA` |
| `exp` | required, in the future (10 s leeway) | required, in the future (10 s leeway) |
| claims checked | none beyond `exp` (bastion is trusted for every service) | `service == "magpie"` and `role == "frontend"` |

Frontend tokens look like
`{"alg":"EdDSA","typ":"JWT","kid":"magpie-frontend-2026-10"}` /
`{"sub":"magpie-frontend","role":"frontend","service":"magpie","iat":…,"exp":iat+60,"jti":…}`.
`kid`, `sub`, `jti` are not checked. Both paths allow a **10 s leeway** on
`exp` and `iat` (see the 2026-10-16 note below).

## Behaviour

1. `DEMO_MODE` on → every request passes (unchanged).
2. Exempt paths (`/health`, `/version`, `/metrics`, `/openapi.json`, `/docs*`,
   `/redoc*`) pass without a token (unchanged).
3. Neither key configured → the middleware fails open (unchanged, documented).
4. At least one key configured → enforcement is on. The token is valid when it
   verifies against bastion's key, **or** against the frontend key with the
   frontend claims above. Otherwise 401 `{"error": "invalid platform token"}`.
5. Only one key configured → only that key is trusted.
6. The frontend key value is read with a leading byte-order mark and edge
   whitespace stripped. An empty value is absent. A value that is not base64 of
   an Ed25519 SPKI public key is **malformed**: the app logs one warning naming
   `MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC` (never the value) and treats the key as
   absent. It never crashes the app or a request.
7. Bastion's key resolution (env, then cached URL) is unchanged; the only change
   on that path is that `exp` is now required. bastion's minter
   (`bastion/src/lib/gateway/jwt.ts`) always sets `exp` to 60 s, so no token it
   issues is affected.

**2026-10-16 — clock skew.** Tokens are minted on Vercel (frontend) or bastion
and verified on Render, and PyJWT rejects an `iat` in the future, so a minter
clock even a second ahead of this host's would 401 a fresh token. Both paths
now decode with `leeway=10`: an `iat` up to 10 s ahead and an `exp` up to 10 s
past are accepted; beyond that the token is rejected. Against a 60 s lifetime
this widens the replay window by at most 10 s.

## Test cases

Pass:

- [x] Frontend token (own service, frontend role, live `exp`) → 200.
- [x] bastion token → 200 with both keys configured.
- [x] Only the frontend key configured → frontend token 200, missing token 401
      (enforcement is on), bastion-signed token 401.
- [x] Frontend key with a BOM and a trailing CRLF → still parsed, token 200.
- [x] Neither key → fails open (existing).
- [x] `DEMO_MODE` → bypass, also with the frontend key configured (existing).
- [x] Exempt path → 200 without a token with the frontend key configured.

Failure:

- [x] Expired frontend token → 401.
- [x] Token signed by an unknown key → 401 with both keys configured.
- [x] Frontend-key token with `service: "paper-trail"` → 401.
- [x] Frontend-key token with `role: "admin"` → 401.
- [x] Frontend-key token without `exp` → 401.
- [x] bastion-key token without `exp` → 401.
- [x] Unsigned (`alg: none`) token with frontend claims → 401.
- [x] Malformed frontend key (not base64; base64 of non-key bytes; a P-256 key)
      → one warning naming the variable and not containing the value; bastion
      tokens still accepted; with no bastion key the middleware fails open.
- [x] The malformed-key warning is logged once, not on every request.

Clock skew (both minters):

- [x] `iat` 5 s in the future → 200; `iat` 30 s in the future → 401.
- [x] `exp` 5 s in the past → 200; `exp` 30 s in the past → 401.

## Rate limiting behind the proxy

`src/magpie/platform/rate_limit.py` keys on `request.client.host`. The
container used to run `uvicorn magpie.main:app --host 0.0.0.0 --port 8000`
with no `--forwarded-allow-ips`, and Render sets no `FORWARDED_ALLOW_IPS`, so
uvicorn trusted `X-Forwarded-For` only from `127.0.0.1`. Behind Render's edge
the socket peer is the edge proxy, so `request.client.host` was the proxy's
address for every caller: bastion, the frontend proxy and everyone else shared
the bucket(s).

**2026-10-16 — trust forwarded addresses.** The Dockerfile `CMD` now adds
`--proxy-headers --forwarded-allow-ips '*'`, exactly as paper-trail-backend's
container does, so `request.client.host` is the leftmost `X-Forwarded-For`
entry: the visitor address the frontend proxy forwards. The limiter code is
unchanged. Forwarded headers are client-supplied, but the token middleware is
installed after the limiter and so runs first: only a caller holding a valid
platform token (bastion or the frontend proxy, which sets the header itself)
reaches the limiter.

- [x] The Dockerfile's exec-form `CMD` carries `--proxy-headers` and
      `--forwarded-allow-ips *` (`tests/unit/test_ops_baseline.py`).
- [x] Behind uvicorn's proxy-headers middleware trusting `*`, two visitors with
      different leftmost `X-Forwarded-For` get separate buckets
      (`tests/unit/test_rate_limit.py`).

## Not changed

- The `X-Platform-Token` header name and the exempt paths.
- The `BASTION_SIGNING_KEY_PUBLIC` / `BASTION_PUBLIC_KEY_URL` resolution and its
  one-hour cache.
- The fail-open-with-no-key behaviour.
