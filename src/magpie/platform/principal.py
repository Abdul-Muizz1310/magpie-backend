"""Who an authenticated request is, as the platform token established it (spec 10).

The token middleware stores a :class:`Principal` on ``request.state``; the rate
limiter reads it through :func:`rate_limit_key`. Anonymous requests (public
routes, ``DEMO_MODE`` without a token, test settings) carry none.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from fastapi import Request

_STATE_ATTR = "platform_principal"


class Minter(enum.StrEnum):
    """Which trusted key verified the token (it also fixed the allowed ``iss``)."""

    bastion = "bastion"
    frontend = "frontend"


@dataclass(frozen=True, slots=True)
class Principal:
    """The verified ``iss``, ``sub`` and ``role`` of a platform token, and its minter."""

    iss: str
    sub: str
    role: str | None
    minter: Minter


def rate_limit_key(principal: Principal | None, client_address: str) -> str:
    """The limiter bucket (contract §2, amended 2026-10-20).

    bastion's ``sub`` is the visitor pseudonym, so it keys the bucket. A frontend's
    ``sub`` is constant (every visitor of the site shares it), so a frontend token
    keys on the forwarded client address its proxy sets. Anonymous: the address.
    """
    if principal is None:
        return client_address
    match principal.minter:
        case Minter.bastion:
            return f"{principal.iss}:{principal.sub}"
        case Minter.frontend:
            return f"{principal.iss}:{client_address}"


def attach_principal(request: Request, principal: Principal) -> None:
    setattr(request.state, _STATE_ATTR, principal)


def principal_of(request: Request) -> Principal | None:
    """The request's principal, or ``None`` when it is anonymous."""
    value = getattr(request.state, _STATE_ATTR, None)
    return value if isinstance(value, Principal) else None


__all__ = ["Minter", "Principal", "attach_principal", "principal_of", "rate_limit_key"]
