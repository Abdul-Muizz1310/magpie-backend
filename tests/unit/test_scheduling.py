"""Tests for per-source cron scheduling (magpie.scheduling)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from magpie.scheduling import InvalidCronError, cron_matches, due_source_names, is_due


class TestCronMatches:
    def test_every_six_hours_fires_at_boundary(self) -> None:
        expr = "0 */6 * * *"  # 00:00, 06:00, 12:00, 18:00
        assert cron_matches(expr, datetime(2026, 7, 4, 6, 0, tzinfo=UTC)) is True
        assert cron_matches(expr, datetime(2026, 7, 4, 12, 0, tzinfo=UTC)) is True
        assert cron_matches(expr, datetime(2026, 7, 4, 7, 0, tzinfo=UTC)) is False
        assert cron_matches(expr, datetime(2026, 7, 4, 6, 30, tzinfo=UTC)) is False

    def test_daily_at_eight(self) -> None:
        expr = "0 8 * * *"
        assert cron_matches(expr, datetime(2026, 7, 4, 8, 0, tzinfo=UTC)) is True
        assert cron_matches(expr, datetime(2026, 7, 4, 9, 0, tzinfo=UTC)) is False

    def test_weekly_sunday_midnight(self) -> None:
        expr = "0 0 * * 0"
        # 2026-07-05 is a Sunday.
        assert cron_matches(expr, datetime(2026, 7, 5, 0, 0, tzinfo=UTC)) is True
        # 2026-07-04 is a Saturday.
        assert cron_matches(expr, datetime(2026, 7, 4, 0, 0, tzinfo=UTC)) is False

    def test_sunday_as_seven(self) -> None:
        assert cron_matches("0 0 * * 7", datetime(2026, 7, 5, 0, 0, tzinfo=UTC)) is True

    def test_comma_list_and_range(self) -> None:
        assert cron_matches("30 9,17 * * 1-5", datetime(2026, 7, 3, 17, 30, tzinfo=UTC)) is True
        # 2026-07-03 is Friday (weekday 5 -> cron 5, in 1-5). 09:30 also matches.
        assert cron_matches("30 9,17 * * 1-5", datetime(2026, 7, 3, 9, 30, tzinfo=UTC)) is True
        # Saturday is excluded.
        assert cron_matches("30 9,17 * * 1-5", datetime(2026, 7, 4, 9, 30, tzinfo=UTC)) is False

    def test_invalid_expr_raises(self) -> None:
        with pytest.raises(InvalidCronError):
            cron_matches("0 0 * *", datetime(2026, 7, 4, tzinfo=UTC))
        with pytest.raises(InvalidCronError):
            cron_matches("bogus 0 * * *", datetime(2026, 7, 4, tzinfo=UTC))


class TestIsDue:
    def test_due_within_hourly_window(self) -> None:
        # At 12:45 with an hourly window, a 12:00 six-hourly cron is "due".
        now = datetime(2026, 7, 4, 12, 45, tzinfo=UTC)
        assert is_due("0 */6 * * *", now, window_seconds=3600) is True

    def test_not_due_outside_window(self) -> None:
        # At 13:30, the 12:00 firing is > 1h ago -> not due for an hourly run.
        now = datetime(2026, 7, 4, 13, 30, tzinfo=UTC)
        assert is_due("0 */6 * * *", now, window_seconds=3600) is False

    def test_weekly_not_due_on_weekday(self) -> None:
        now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)  # Saturday
        assert is_due("0 0 * * 0", now, window_seconds=3600) is False


class TestDueSourceNames:
    def test_filters_by_schedule(self) -> None:
        from magpie.config.schema import SourceConfig

        def _cfg(name: str, schedule: str) -> SourceConfig:
            return SourceConfig(
                name=name,
                url="https://example.com",  # type: ignore[arg-type]
                schedule=schedule,
                item={  # type: ignore[arg-type]
                    "container": "div",
                    "fields": [{"name": "id", "selector": "::attr(id)"}],
                    "dedupe_key": "id",
                },
            )

        configs = [
            _cfg("sixhourly", "0 */6 * * *"),
            _cfg("weekly", "0 0 * * 0"),
        ]
        now = datetime(2026, 7, 4, 12, 30, tzinfo=UTC)  # Saturday, 12:xx
        assert due_source_names(configs, now, window_seconds=3600) == ["sixhourly"]
