"""What one heal attempt on one source produced: fixes that landed, and failures.

A heal ends healed or failed, never neither (spec 03, 2026-10-20). On 2026-10-19
every OpenRouter call answered 404 and the heal job still finished green with
``{'healed': []}``; a summary that carries no fix must carry the reason, and the
type refuses to exist otherwise.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from magpie.storage.models import HealMode, SourceOrigin

SOURCE_TARGET = "source"
"""The ``target`` of a failure that concerns the whole source, not one selector."""


class HealedTarget(BaseModel):
    """One container/field selector whose fix landed (PR opened, or config patched)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str = Field(min_length=1)
    mode: HealMode
    applied: bool
    pr_url: str | None
    new_selector: str = Field(min_length=1)


class HealFailure(BaseModel):
    """One target that did not heal, and why (``reason`` prefixes are in spec 03)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class HealSummary(BaseModel):
    """The outcome of ``heal_source`` for one source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    origin: SourceOrigin | None
    healed: tuple[HealedTarget, ...] = ()
    failures: tuple[HealFailure, ...] = ()

    @model_validator(mode="after")
    def _healed_or_failed(self) -> Self:
        if not self.healed and not self.failures:
            msg = f"heal summary for {self.source!r} neither healed nor failed"
            raise ValueError(msg)
        return self

    @property
    def succeeded(self) -> bool:
        """True iff nothing failed — and so, by the invariant, something healed."""
        return not self.failures

    def describe_failures(self) -> str:
        """``target: reason`` per failure, ``; ``-joined, for one ERROR log line."""
        return "; ".join(f"{f.target}: {f.reason}" for f in self.failures)


__all__ = ["SOURCE_TARGET", "HealFailure", "HealSummary", "HealedTarget"]
