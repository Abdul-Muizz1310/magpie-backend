"""The published heal-rate number must reproduce (spec 07).

Runs the committed case suite end-to-end through the shipped extractor, detector,
validator and YAML patcher, then pins the result against both the committed
``evals/heal_rate.json`` baseline and the figure quoted in the README. No Docker,
no network — this belongs to the fast tier on purpose: a claim that only
reproduces under special conditions is not reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from magpie.evals.cli import format_report, main
from magpie.evals.heal_rate import load_cases, run_suite

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "evals" / "heal_rate_cases.yaml"
BASELINE = REPO_ROOT / "evals" / "heal_rate.json"
README = REPO_ROOT / "README.md"


@pytest.fixture(scope="module")
def report():  # type: ignore[no-untyped-def]
    return run_suite(load_cases(MANIFEST, root=REPO_ROOT))


@pytest.fixture(scope="module")
def baseline() -> dict[str, object]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


class TestEveryCaseBehavesAsDeclared:
    def test_no_expectation_mismatches(self, report) -> None:  # type: ignore[no-untyped-def]
        assert report.expectation_failures == (), (
            "a case healed or missed against its declared expectation:\n" + format_report(report)
        )

    def test_suite_covers_both_outcomes(self, report) -> None:  # type: ignore[no-untyped-def]
        """A suite of only-healable cases would make the rate a tautology."""
        outcomes = {case.expect_healable for case in report.cases}
        assert outcomes == {True, False}

    def test_every_case_was_genuinely_broken_first(self, report) -> None:  # type: ignore[no-untyped-def]
        for case in report.cases:
            assert case.breakage_kind in {"container", "fields", "underflow"}
            assert case.items_before > 0

    def test_container_breaks_extracted_nothing_before_healing(self, report) -> None:  # type: ignore[no-untyped-def]
        container_cases = [c for c in report.cases if c.breakage_kind == "container"]
        assert container_cases, "the suite should exercise a container break"
        for case in container_cases:
            assert case.items_broken == 0
            assert case.items_healed == case.items_before

    def test_healed_cases_all_recorded_a_proposal(self, report) -> None:  # type: ignore[no-untyped-def]
        for case in report.cases:
            if case.healed:
                assert case.proposals, f"{case.name} healed with no selector change"
                for proposal in case.proposals:
                    assert proposal.new_selector != proposal.old_selector

    def test_unhealable_case_declines_rather_than_guessing(self, report) -> None:  # type: ignore[no-untyped-def]
        misses = [c for c in report.cases if not c.healed]
        assert misses
        for case in misses:
            assert case.proposals == (), f"{case.name} proposed a fix but stayed broken"


class TestCommittedBaselineStillHolds:
    def test_rate_matches_the_committed_baseline(
        self,
        report,  # type: ignore[no-untyped-def]
        baseline: dict[str, object],
    ) -> None:
        assert report.total == baseline["total_cases"]
        assert report.healed == baseline["healed_cases"]
        assert report.heal_rate_pct == baseline["heal_rate_pct"]

    def test_per_case_detail_matches_the_committed_baseline(
        self,
        report,  # type: ignore[no-untyped-def]
        baseline: dict[str, object],
    ) -> None:
        """Whole-report equality, so a changed selector proposal can't hide."""
        assert report.as_dict() == baseline, "run `uv run magpie-eval --write` to refresh"


class TestPublishedNumber:
    def test_readme_quotes_the_measured_rate(self, report) -> None:  # type: ignore[no-untyped-def]
        text = README.read_text(encoding="utf-8")
        measured = f"{report.healed}/{report.total}"
        assert measured in text, f"README should state the measured heal rate {measured}"
        assert f"{report.heal_rate_pct:.2f}%" in text

    def test_cli_exits_zero_when_the_suite_is_consistent(self) -> None:
        assert main([]) == 0
