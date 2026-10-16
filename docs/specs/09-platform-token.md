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
| `exp` | required, in the future | required, in the future |
| claims checked | none beyond `exp` (bastion is trusted for every service) | `service == "magpie"` and `role == "frontend"` |

Frontend tokens look like
`{"alg":"EdDSA","typ":"JWT","kid":"magpie-frontend-2026-10"}` /
`{"sub":"magpie-frontend","role":"frontend","service":"magpie","iat":…,"exp":iat+60,"jti":…}`.
`kid`, `sub`, `jti` are not checked. No leeway is applied to `exp` or `iat`,
matching the bastion path.

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

## Rate limiting behind the proxy (unchanged)

`src/magpie/platform/rate_limit.py` keys on `request.client.host`. The
container runs `uvicorn magpie.main:app --host 0.0.0.0 --port 8000` with no
`--forwarded-allow-ips` and Render sets no `FORWARDED_ALLOW_IPS`, so uvicorn
trusts `X-Forwarded-For` only from `127.0.0.1`. Behind Render's edge the socket
peer is the edge proxy, so `request.client.host` is the proxy's address for
every caller — bastion, the frontend proxy and anyone else share the bucket(s).
The frontend's forwarded `X-Forwarded-For` does not change that. This spec does
not change the limiter; see the report on DR-0030 for the options.

## Not changed

- The `X-Platform-Token` header name and the exempt paths.
- The `BASTION_SIGNING_KEY_PUBLIC` / `BASTION_PUBLIC_KEY_URL` resolution and its
  one-hour cache.
- The fail-open-with-no-key behaviour.
