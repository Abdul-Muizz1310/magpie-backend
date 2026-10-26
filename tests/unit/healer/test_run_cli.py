"""Tests for ``python -m magpie.healer.run`` — the heal-on-failure job's entrypoint.

The job exits non-zero when any heal target ends without a landed fix
(spec 03, "A heal ends healed or failed, never silent").
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import yaml

import magpie.healer.run as heal_cli
from magpie.config.schema import SourceConfig
from magpie.healer.outcome import HealedTarget, HealFailure, HealSummary
from magpie.storage.models import HealMode, SourceOrigin
from magpie.storage.runs_repo_pg import PgRunRepository
from magpie.storage.sources_repo import SourcesRepository

SAMPLE_YAML = """\
name: heal-cli-src
url: https://example.com
schedule: "0 */6 * * *"
item:
  container: "div"
  fields:
    - { name: title, selector: "h2::text" }
    - { name: id, selector: "::attr(data-id)" }
  dedupe_key: id
health:
  min_items: 5
"""

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _healed(source: str) -> HealSummary:
    return HealSummary(
        source=source,
        origin=SourceOrigin.file,
        healed=(
            HealedTarget(
                target="container",
                mode=HealMode.pr,
                applied=False,
                pr_url=f"https://github.com/owner/repo/pull/{len(source)}",
                new_selector="dl#articles > dd",
            ),
        ),
    )


def _failed(source: str, reason: str = "LLM error: HTTPStatusError: 404 Not Found") -> HealSummary:
    return HealSummary(
        source=source,
        origin=SourceOrigin.file,
        failures=(HealFailure(target="container", reason=reason),),
    )


async def _seed_source(session_factory, name: str) -> None:
    cfg = SourceConfig(**yaml.safe_load(SAMPLE_YAML.replace("heal-cli-src", name)))
    async with session_factory() as session:
        await SourcesRepository(session).create(
            config=cfg,
            origin=SourceOrigin.file,
            yaml_text=yaml.safe_dump(cfg.model_dump(mode="json")),
        )
        await session.commit()


async def _add_run(
    session_factory, name: str, *, at: datetime, error: str | None = None, items: int = 0
) -> None:
    """Append one finished run for ``name`` that started at ``at``."""
    async with session_factory() as session:
        src = await SourcesRepository(session).get_by_name(name)
        assert src is not None
        repo = PgRunRepository(session)
        run = await repo.create_queued(source_id=src.id, source_name=name)
        run.started_at = at
        if error is not None:
            await repo.mark_error(run.id, error=error, started_at=at)
        else:
            await repo.mark_ok(
                run.id,
                item_count=items,
                items_new=items,
                items_updated=0,
                items_removed=0,
                started_at=at,
            )
        await session.commit()


def _run_main(session_factory, monkeypatch, summaries: dict[str, HealSummary]):
    """Run ``main`` with ``heal_source`` answering from ``summaries``; return (rc, healed)."""
    monkeypatch.setattr(heal_cli, "get_session_factory", lambda: session_factory)
    called: list[str] = []

    async def _fake_heal(*, source, run_id, session_factory, trigger):  # type: ignore[no-untyped-def]
        assert trigger == "cli"  # spec 12: the heal job's traces are tagged trigger:cli
        called.append(source)
        return summaries[source]

    with patch("magpie.healer.run.heal_source", new=AsyncMock(side_effect=_fake_heal)):
        rc = heal_cli.main([])
    return rc, called


@pytest.fixture(autouse=True)
def _no_dispatch_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEAL_SOURCE_FILTER", raising=False)


class TestTargets:
    def test_latest_error_run_is_a_target(self, session_factory, monkeypatch) -> None:
        asyncio.run(_seed_source(session_factory, "errored"))
        asyncio.run(_add_run(session_factory, "errored", at=T0, error="boom"))

        rc, called = _run_main(session_factory, monkeypatch, {"errored": _healed("errored")})

        assert called == ["errored"]
        assert rc == 0

    def test_latest_ok_run_that_underflowed_is_a_target(self, session_factory, monkeypatch) -> None:
        """A run marked ``ok`` but below min_items is silent drift (MAG-1)."""
        asyncio.run(_seed_source(session_factory, "underflow-src"))
        asyncio.run(_add_run(session_factory, "underflow-src", at=T0, items=1))

        rc, called = _run_main(
            session_factory, monkeypatch, {"underflow-src": _healed("underflow-src")}
        )

        assert called == ["underflow-src"]
        assert rc == 0

    def test_old_error_with_a_newer_healthy_run_is_not_a_target(
        self, session_factory, monkeypatch
    ) -> None:
        """The 2026-10-19 job re-healed every source that had *ever* errored."""
        asyncio.run(_seed_source(session_factory, "recovered"))
        asyncio.run(_add_run(session_factory, "recovered", at=T0, error="old outage"))
        asyncio.run(_add_run(session_factory, "recovered", at=T0 + timedelta(days=1), items=30))

        rc, called = _run_main(session_factory, monkeypatch, {})

        assert called == []
        assert rc == 0

    def test_healthy_run_at_min_items_is_not_a_target(self, session_factory, monkeypatch) -> None:
        asyncio.run(_seed_source(session_factory, "exactly-enough"))
        asyncio.run(_add_run(session_factory, "exactly-enough", at=T0, items=5))

        rc, called = _run_main(session_factory, monkeypatch, {})

        assert (rc, called) == (0, [])

    def test_no_runs_at_all_means_no_target(self, session_factory, monkeypatch, capsys) -> None:
        asyncio.run(_seed_source(session_factory, "never-ran"))

        rc, called = _run_main(session_factory, monkeypatch, {})

        assert (rc, called) == (0, [])
        assert "no failed or underflowed runs to heal" in capsys.readouterr().out


class TestExitCode:
    def test_a_failed_heal_exits_one_and_logs_error(
        self, session_factory, monkeypatch, caplog
    ) -> None:
        asyncio.run(_seed_source(session_factory, "arxiv-cs"))
        asyncio.run(_add_run(session_factory, "arxiv-cs", at=T0, items=0))

        with caplog.at_level(logging.ERROR, logger="magpie.healer.run"):
            rc, _ = _run_main(session_factory, monkeypatch, {"arxiv-cs": _failed("arxiv-cs")})

        assert rc == 1
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("arxiv-cs" in m and "404" in m for m in errors), errors

    def test_one_failure_among_successes_still_exits_one(
        self, session_factory, monkeypatch, caplog
    ) -> None:
        for name in ("good-src", "bad-src"):
            asyncio.run(_seed_source(session_factory, name))
            asyncio.run(_add_run(session_factory, name, at=T0, error="boom"))

        with caplog.at_level(logging.ERROR, logger="magpie.healer.run"):
            rc, called = _run_main(
                session_factory,
                monkeypatch,
                {"good-src": _healed("good-src"), "bad-src": _failed("bad-src")},
            )

        assert sorted(called) == ["bad-src", "good-src"]
        assert rc == 1
        errors = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
        assert "bad-src" in errors
        assert "good-src" not in errors.replace("bad-src", "")


class TestManualDispatch:
    def _dispatch(self, monkeypatch, summary: HealSummary) -> int:
        monkeypatch.setenv("HEAL_SOURCE_FILTER", f"  {summary.source}  ")
        monkeypatch.setattr(heal_cli, "get_session_factory", lambda: None)
        with patch("magpie.healer.run.heal_source", new=AsyncMock(return_value=summary)) as heal:
            rc = heal_cli.main([])
        heal.assert_awaited_once()
        assert heal.await_args is not None
        assert heal.await_args.kwargs["source"] == summary.source
        assert heal.await_args.kwargs["run_id"] is None
        return rc

    def test_dispatch_that_heals_exits_zero(self, monkeypatch) -> None:
        assert self._dispatch(monkeypatch, _healed("arxiv-cs")) == 0

    def test_dispatch_that_heals_nothing_exits_one(self, monkeypatch, caplog) -> None:
        nothing = _failed("arxiv-cs", reason="nothing to heal: the live page yields 50 items")
        with caplog.at_level(logging.ERROR, logger="magpie.healer.run"):
            rc = self._dispatch(monkeypatch, nothing)
        assert rc == 1
        assert any("nothing to heal" in r.getMessage() for r in caplog.records)
