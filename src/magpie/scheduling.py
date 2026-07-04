"""Per-source cron scheduling — honour each source's declared ``schedule``.

Historically the ``schedule`` field on every ``SourceConfig`` was decorative:
one global weekly GitHub Actions cron fanned out to *all* sources regardless of
their declared cadence, so a source asking for ``0 */6 * * *`` (every 6h) still
only ran weekly. This module makes the field real: the workflow runs hourly and
``magpie due`` filters the matrix to the sources whose cron actually fires in
the current window.

Supports standard 5-field cron (``minute hour day-of-month month day-of-week``)
with ``*``, ``a``, ``a-b``, ``*/n``, ``a-b/n`` and comma lists — the full syntax
the shipped configs use. Sunday is both ``0`` and ``7`` for day-of-week.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from magpie.config.schema import SourceConfig

_FIELD_BOUNDS: tuple[tuple[int, int], ...] = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 7),  # day of week (0 and 7 both == Sunday)
)


class InvalidCronError(ValueError):
    """Raised when a cron expression can't be parsed."""


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field into the concrete set of matching integers."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise InvalidCronError(f"empty cron field component in {field!r}")
        step = 1
        if "/" in part:
            base, _, step_str = part.partition("/")
            try:
                step = int(step_str)
            except ValueError as exc:
                raise InvalidCronError(f"bad step in {part!r}") from exc
            if step <= 0:
                raise InvalidCronError(f"non-positive step in {part!r}")
        else:
            base = part

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_str, _, end_str = base.partition("-")
            try:
                start, end = int(start_str), int(end_str)
            except ValueError as exc:
                raise InvalidCronError(f"bad range in {base!r}") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise InvalidCronError(f"bad value in {base!r}") from exc

        if start > end:
            raise InvalidCronError(f"range start > end in {base!r}")
        for v in range(start, end + 1, step):
            if not (lo <= v <= hi):
                raise InvalidCronError(f"value {v} out of bounds [{lo},{hi}] in {field!r}")
            values.add(v)
    return values


def cron_matches(expr: str, dt: datetime) -> bool:
    """Return True if ``expr`` fires at ``dt`` (truncated to the minute)."""
    fields = expr.split()
    if len(fields) != 5:
        raise InvalidCronError(f"expected 5 cron fields, got {len(fields)}: {expr!r}")

    minute, hour, dom, month, dow = (
        _parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELD_BOUNDS, strict=True)
    )

    # Normalise 7 (Sunday alias) to 0 so range checks below use a single form.
    if 7 in dow:
        dow = (dow - {7}) | {0}

    # Python weekday(): Mon=0..Sun=6. Cron: Sun=0..Sat=6.
    cron_dow = (dt.weekday() + 1) % 7
    dow_match = cron_dow in dow

    # Standard cron semantics: when both DOM and DOW are restricted (not "*"),
    # a match on *either* qualifies. Here we treat a field as unrestricted only
    # when it covers its whole range.
    dom_restricted = dom != set(range(1, 32))
    dow_restricted = dow != set(range(0, 7))
    if dom_restricted and dow_restricted:
        day_match = dt.day in dom or dow_match
    else:
        day_match = dt.day in dom and dow_match

    return dt.minute in minute and dt.hour in hour and dt.month in month and day_match


def is_due(expr: str, now: datetime, window_seconds: int) -> bool:
    """Did ``expr`` fire at any minute within the ``window_seconds`` ending at ``now``?

    The workflow runs on a fixed cadence (hourly); ``window_seconds`` should be
    that cadence so a source is considered due exactly once per firing.
    """
    minutes = max(1, window_seconds // 60)
    base = now.replace(second=0, microsecond=0)
    return any(cron_matches(expr, base - timedelta(minutes=i)) for i in range(minutes))


def due_source_names(
    configs: Iterable[SourceConfig], now: datetime, window_seconds: int
) -> list[str]:
    """Names of the sources whose ``schedule`` fires within the window."""
    return [c.name for c in configs if is_due(c.schedule, now, window_seconds)]


__all__ = ["InvalidCronError", "cron_matches", "due_source_names", "is_due"]
