"""X-Platform-Token middleware — platform token contract v2 (docs/specs/10-platform-contract-v2.md).

The workspace contract (``docs/specs/platform-contract-v2.md``, DR-0033) fixes the
rules; this module is magpie's copy (DR-0006). In short:

- Two trusted Ed25519 keys: bastion's (``BASTION_SIGNING_KEY_PUBLIC``, else fetched
  from ``BASTION_PUBLIC_KEY_URL`` and cached for an hour) and magpie-frontend's own
  (``MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC``). **The key decides the issuer**: bastion's
  key admits only ``iss == "bastion"``; the frontend key admits only
  ``iss == "magpie-frontend"`` with ``role == "frontend"``.
- ``aud`` must be the string ``"magpie"``; ``exp``/``iat`` are integers checked with
  a 10 s leeway, and the lifetime is at most 300 s.
- Missing token: 401 ``missing_platform_token``, unless ``DEMO_MODE`` (default false)
  lets it through anonymous with a WARNING. An invalid token is always 401
  ``invalid_platform_token``, with a WARNING naming the failed check, never the token.
- No trusted key: 503 ``platform_auth_unconfigured`` on every non-public route, except
  under the test settings (``APP_ENV=test``).
"""

from __future__ import annotations

import base64
import binascii
import enum
import functools
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Self, TypeGuard

import httpx
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from magpie.platform.principal import Principal, attach_principal

logger = logging.getLogger(__name__)

_Handler = Callable[[Request], Awaitable[Response]]

SERVICE_ID: Final = "magpie"
BASTION_ISS: Final = "bastion"
FRONTEND_ISS: Final = "magpie-frontend"
FRONTEND_ROLE: Final = "frontend"
LEEWAY_S: Final = 10
MAX_LIFETIME_S: Final = 300

TOKEN_HEADER: Final = "x-platform-token"
BASTION_KEY_ENV: Final = "BASTION_SIGNING_KEY_PUBLIC"
BASTION_KEY_URL_ENV: Final = "BASTION_PUBLIC_KEY_URL"
FRONTEND_KEY_ENV: Final = "MAGPIE_FRONTEND_SIGNING_KEY_PUBLIC"

_PUBLIC_EXACT = frozenset({"/health", "/version", "/metrics", "/openapi.json", "/docs", "/redoc"})
_PUBLIC_PREFIXES = ("/docs/", "/redoc/")
_PUBLIC_KEY_TTL_S = 3600.0
_BOM = "﻿"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PlatformAuthSettings:
    """``demo_mode``: a missing token proceeds anonymous. ``test_settings``: no key => pass."""

    demo_mode: bool
    test_settings: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Self:
        demo = environ.get("DEMO_MODE", "false").strip().lower() in _TRUTHY
        test = environ.get("APP_ENV", "").strip().lower() == "test"
        return cls(demo_mode=demo, test_settings=test)


def is_public_path(path: str) -> bool:
    """Routes that never need a token (contract §2); magpie has no verification surface."""
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)


# ── Pure verification core ────────────────────────────────────────────────────


class TokenCheck(enum.StrEnum):
    """The check a rejected token failed — named in the WARNING log."""

    malformed = "malformed"
    alg = "alg"
    signature = "signature"
    iss = "iss"
    role = "role"
    aud = "aud"
    exp = "exp"
    iat = "iat"
    lifetime = "lifetime"
    sub = "sub"


class InvalidPlatformTokenError(Exception):
    def __init__(self, check: TokenCheck) -> None:
        super().__init__(check.value)
        self.check = check


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def check_claims(claims: Mapping[str, Any], *, expected_iss: str, now: float) -> Principal:
    """Validate the claims of a token whose signature ``expected_iss``'s key verified."""
    if claims.get("iss") != expected_iss:
        raise InvalidPlatformTokenError(TokenCheck.iss)
    role = claims.get("role")
    if expected_iss == FRONTEND_ISS and role != FRONTEND_ROLE:
        raise InvalidPlatformTokenError(TokenCheck.role)
    aud = claims.get("aud")
    if not isinstance(aud, str) or aud != SERVICE_ID:
        raise InvalidPlatformTokenError(TokenCheck.aud)
    exp, iat = claims.get("exp"), claims.get("iat")
    if not _is_int(exp):
        raise InvalidPlatformTokenError(TokenCheck.exp)
    if not _is_int(iat):
        raise InvalidPlatformTokenError(TokenCheck.iat)
    if not exp + LEEWAY_S > now:
        raise InvalidPlatformTokenError(TokenCheck.exp)
    if not iat - LEEWAY_S <= now:
        raise InvalidPlatformTokenError(TokenCheck.iat)
    if not 0 < exp - iat <= MAX_LIFETIME_S:
        raise InvalidPlatformTokenError(TokenCheck.lifetime)
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidPlatformTokenError(TokenCheck.sub)
    return Principal(iss=expected_iss, sub=sub, role=role if isinstance(role, str) else None)


def _verified_claims(token: str, key: Ed25519PublicKey) -> dict[str, Any] | None:
    """The token's claims if ``key`` verifies its EdDSA signature, else ``None``."""
    try:
        payload = jwt.PyJWS().decode_complete(token, key, algorithms=["EdDSA"])["payload"]
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return None
    try:
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidPlatformTokenError(TokenCheck.malformed) from exc
    if not isinstance(claims, dict):
        raise InvalidPlatformTokenError(TokenCheck.malformed)
    return claims


def verify_platform_token(
    token: str,
    *,
    bastion_key: Ed25519PublicKey | None,
    frontend_key: Ed25519PublicKey | None,
    now: float,
) -> Principal:
    """The token's principal, or :class:`InvalidPlatformTokenError` naming the failed check."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise InvalidPlatformTokenError(TokenCheck.malformed) from exc
    if header.get("alg") != "EdDSA":
        raise InvalidPlatformTokenError(TokenCheck.alg)
    for key, issuer in ((bastion_key, BASTION_ISS), (frontend_key, FRONTEND_ISS)):
        if key is None:
            continue
        claims = _verified_claims(token, key)
        if claims is not None:
            return check_claims(claims, expected_iss=issuer, now=now)
    raise InvalidPlatformTokenError(TokenCheck.signature)


# ── Trusted keys (imperative shell) ───────────────────────────────────────────

# (fetched_at_monotonic, key) — only used for the BASTION_PUBLIC_KEY_URL path.
_key_cache: tuple[float, Ed25519PublicKey | None] = (0.0, None)

# Module-level async client reused across requests so the key fetch never blocks
# the event loop (P10 / MAG-7). Lazily created inside the running loop.
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=5.0)
    return _http_client


async def aclose_http_client() -> None:
    """Dispose the shared async client (graceful shutdown / test seam)."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def reset_public_key_cache() -> None:
    """Clear the fetched-key cache and the parsed env keys (test seam)."""
    global _key_cache
    _key_cache = (0.0, None)
    _parse_named_key.cache_clear()


def parse_ed25519_public_key(b64_der: str) -> Ed25519PublicKey | None:
    """Parse base64 SPKI DER into an Ed25519 public key; ``None`` for anything else."""
    try:
        key = serialization.load_der_public_key(base64.b64decode(b64_der, validate=True))
    except (ValueError, binascii.Error, UnsupportedAlgorithm):
        return None
    return key if isinstance(key, Ed25519PublicKey) else None


def _clean(value: str) -> str:
    """Strip a byte-order mark and edge whitespace (values pasted from a Windows shell)."""
    return value.strip().lstrip(_BOM).strip()


@functools.lru_cache(maxsize=8)
def _parse_named_key(source: str, value: str) -> Ed25519PublicKey | None:
    """Parse one configured value; cached so a bad value warns once, not per request."""
    key = parse_ed25519_public_key(value)
    if key is None:
        # Name the source, never the value.
        logger.warning("%s is not a base64 Ed25519 public key (SPKI DER); ignoring it", source)
    return key


def _env_key(name: str) -> Ed25519PublicKey | None:
    value = _clean(os.environ.get(name, ""))
    return _parse_named_key(name, value) if value else None


async def load_bastion_key() -> Ed25519PublicKey | None:
    """bastion's key: the env value first, else the cached ``BASTION_PUBLIC_KEY_URL`` fetch."""
    if _clean(os.environ.get(BASTION_KEY_ENV, "")):
        return _env_key(BASTION_KEY_ENV)
    url = os.environ.get(BASTION_KEY_URL_ENV, "").strip()
    if not url:
        return None

    global _key_cache
    cached_at, cached = _key_cache
    now = time.monotonic()
    if cached is not None and (now - cached_at) < _PUBLIC_KEY_TTL_S:
        return cached
    try:
        resp = await _get_http_client().get(url)
        resp.raise_for_status()
        raw = str(resp.json()["publicKey"])
    except Exception:  # pragma: no cover - network failure path
        logger.warning("could not fetch bastion public key from %s", url)
        return None
    key = _parse_named_key(BASTION_KEY_URL_ENV, _clean(raw))
    if key is not None:
        _key_cache = (now, key)
    return key


def load_frontend_key() -> Ed25519PublicKey | None:
    """magpie-frontend's key from its env var (no URL fetch)."""
    return _env_key(FRONTEND_KEY_ENV)


# ── Middleware ────────────────────────────────────────────────────────────────


def _extract_token(request: Request) -> str | None:
    """``X-Platform-Token`` first, then ``Authorization: Bearer``; blank means absent."""
    header = request.headers.get(TOKEN_HEADER, "").strip()
    if header:
        return header
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    return None


def _detail(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)


def install_platform_token(app: FastAPI, *, settings: PlatformAuthSettings) -> None:
    """Attach the contract-v2 token middleware to ``app``."""
    if settings.demo_mode:
        logger.warning(
            "DEMO_MODE active: requests without a platform token proceed anonymous; "
            "invalid tokens are still rejected"
        )

    @app.middleware("http")
    async def _platform_token_middleware(request: Request, call_next: _Handler) -> Response:
        path = request.url.path
        if is_public_path(path):
            return await call_next(request)

        bastion_key = await load_bastion_key()
        frontend_key = load_frontend_key()
        if bastion_key is None and frontend_key is None:
            if settings.test_settings:
                return await call_next(request)
            logger.warning(
                "no trusted platform key configured; refusing %s %s", request.method, path
            )
            return _detail(503, "platform_auth_unconfigured")

        token = _extract_token(request)
        if token is None:
            if settings.demo_mode:
                logger.warning(
                    "DEMO_MODE: %s %s has no platform token; proceeding anonymous",
                    request.method,
                    path,
                )
                return await call_next(request)
            return _detail(401, "missing_platform_token")

        try:
            principal = verify_platform_token(
                token, bastion_key=bastion_key, frontend_key=frontend_key, now=time.time()
            )
        except InvalidPlatformTokenError as exc:
            logger.warning(
                "rejected platform token on %s %s: failed check %r",
                request.method,
                path,
                exc.check.value,
            )
            return _detail(401, "invalid_platform_token")

        attach_principal(request, principal)
        return await call_next(request)


__all__ = [
    "BASTION_ISS",
    "FRONTEND_ISS",
    "SERVICE_ID",
    "InvalidPlatformTokenError",
    "PlatformAuthSettings",
    "TokenCheck",
    "aclose_http_client",
    "check_claims",
    "install_platform_token",
    "is_public_path",
    "load_bastion_key",
    "load_frontend_key",
    "parse_ed25519_public_key",
    "reset_public_key_cache",
    "verify_platform_token",
]
