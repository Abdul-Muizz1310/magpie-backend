"""Who an authenticated request is, as the platform token established it (spec 10).

The token middleware stores a :class:`Principal` on ``request.state``; the rate
limiter reads it to key buckets on ``"{iss}:{sub}"``. Anonymous requests (public
routes, ``DEMO_MODE`` without a token, test settings) carry none.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

_STATE_ATTR = "platform_principal"


@dataclass(frozen=True, slots=True)
class Principal:
    """The verified ``iss``, ``sub`` and ``role`` of a platform token."""

    iss: str
    sub: str
    role: str | None

    @property
    def rate_limit_key(self) -> str:
        return f"{self.iss}:{self.sub}"


def attach_principal(request: Request, principal: Principal) -> None:
    setattr(request.state, _STATE_ATTR, principal)


def principal_of(request: Request) -> Principal | None:
    """The request's principal, or ``None`` when it is anonymous."""
    value = getattr(request.state, _STATE_ATTR, None)
    return value if isinstance(value, Principal) else None


__all__ = ["Principal", "attach_principal", "principal_of"]
