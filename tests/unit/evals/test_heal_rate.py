"""Spec 07 — heal-rate eval driver (breakage detection, two-stage heal, scoring)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from magpie.config.schema import SourceConfig
from magpie.evals.heal_rate import (
    CaseResult,
    EmptyEvalSuiteError,
    HealRateReport,
    InvalidEvalCaseError,
    detect_breakage,
    evaluate_case,
    field_samples,
    heal_offline,
    load_cases,
    run_suite,
)

CONFIG_YAML = """\
name: demo
url: https://example.test
schedule: "0 0 * * 0"
item:
  container: "tr.athing"
  fields:
    - { name: title, selector: "span.titleline > a::text" }
    - { name: url, selector: "span.titleline > a::attr(href)" }
    - { name: id, selector: "::attr(id)" }
  dedupe_key: id
health:
  min_items: 1
"""

HEALTHY = """\
<html><body><table>
  <tr class="athing" id="1">
    <td class="title"><span class="titleline"><a href="/a1">Alpha</a></span></td>
  </tr>
  <tr class="athing" id="2">
    <td class="title"><span class="titleline"><a href="/a2">Beta</a></span></td>
  </tr>
</table></body></html>
"""

FIELD_BROKEN = HEALTHY.replace("titleline", "storylink")

CONTAINER_BROKEN = HEALTHY.replace('class="athing"', 'class="story-row"')

# Both the row class and the title wrapper class drifted in one redesign.
CONTAINER_AND_FIELD_BROKEN = HEALTHY.replace('class="athing"', 'class="story-row"').replace(
    "titleline", "storylink"
)

# The title text is simply gone from the page — nothing to anchor on.
UNHEALABLE = """\
<html><body><table>
  <tr class="athing" id="1"><td class="title"><span class="titleline"><a href="/a1"></a></span></td></tr>
  <tr class="athing" id="2"><td class="title"><span class="titleline"><a href="/a2"></a></span></td></tr>
</table></body></html>
"""


@pytest.fixture
def config() -> SourceConfig:
    return SourceConfig(**yaml.safe_load(CONFIG_YAML))


class TestDetectBreakage:
    def test_healthy_page_reports_no_breakage(self, config: SourceConfig) -> None:
        breakage = detect_breakage(config, HEALTHY, min_items=1)
        assert breakage.kind == "none"
        assert breakage.is_broken is False
        assert breakage.item_count == 2

    def test_zero_items_is_a_container_break(self, config: SourceConfig) -> None:
        breakage = detect_breakage(config, CONTAINER_BROKEN, min_items=1)
        assert breakage.kind == "container"
        assert breakage.item_count == 0

    def test_all_none_field_is_a_field_break(self, config: SourceConfig) -> None:
        breakage = detect_breakage(config, FIELD_BROKEN, min_items=1)
        assert breakage.kind == "fields"
        assert set(breakage.fields) == {"title", "url"}

    def test_too_few_items_is_an_underflow(self, config: SourceConfig) -> None:
        breakage = detect_breakage(config, HEALTHY, min_items=5)
        assert breakage.kind == "underflow"
        assert breakage.is_broken is True


class TestFieldSamples:
    def test_collects_the_values_each_field_used_to_extract(self, config: SourceConfig) -> None:
        samples = field_samples(config, HEALTHY)
        assert samples["title"] == ["Alpha", "Beta"]
        assert samples["url"] == ["/a1", "/a2"]
        assert samples["id"] == ["1", "2"]

    def test_omits_fields_that_extract_nothing(self, config: SourceConfig) -> None:
        samples = field_samples(config, FIELD_BROKEN)
        assert "title" not in samples
        assert samples["id"] == ["1", "2"]


class TestHealOffline:
    def test_repairs_a_field_break(self, config: SourceConfig) -> None:
        attempt = heal_offline(config=config, before_html=HEALTHY, after_html=FIELD_BROKEN)
        assert {p.target for p in attempt.proposals} == {"title", "url"}
        assert detect_breakage(attempt.config, FIELD_BROKEN, min_items=1).kind == "none"

    def test_repairs_a_container_break(self, config: SourceConfig) -> None:
        attempt = heal_offline(config=config, before_html=HEALTHY, after_html=CONTAINER_BROKEN)
        assert attempt.proposals[0].target == "container"
        assert attempt.config.item.container == "tr.story-row"
        assert detect_breakage(attempt.config, CONTAINER_BROKEN, min_items=1).kind == "none"

    def test_repairs_container_and_fields_in_two_stages(self, config: SourceConfig) -> None:
        attempt = heal_offline(
            config=config, before_html=HEALTHY, after_html=CONTAINER_AND_FIELD_BROKEN
        )
        targets = [p.target for p in attempt.proposals]
        assert targets[0] == "container"
        assert {"title", "url"} <= set(targets[1:])
        assert (
            detect_breakage(attempt.config, CONTAINER_AND_FIELD_BROKEN, min_items=1).kind == "none"
        )

    def test_proposes_nothing_when_the_values_are_gone(self, config: SourceConfig) -> None:
        attempt = heal_offline(config=config, before_html=HEALTHY, after_html=UNHEALABLE)
        assert [p.target for p in attempt.proposals] == []
        assert attempt.config == config

    def test_healthy_page_needs_no_proposals(self, config: SourceConfig) -> None:
        attempt = heal_offline(config=config, before_html=HEALTHY, after_html=HEALTHY)
        assert attempt.proposals == ()


class TestEvaluateCase:
    def _evaluate(self, config: SourceConfig, after: str, *, expect: bool) -> CaseResult:
        return evaluate_case(
            name="demo",
            description="unit",
            config=config,
            before_html=HEALTHY,
            after_html=after,
            min_items=1,
            expect_healable=expect,
        )

    def test_healed_case_is_scored_as_a_hit(self, config: SourceConfig) -> None:
        result = self._evaluate(config, FIELD_BROKEN, expect=True)
        assert result.healed is True
        assert result.matches_expectation is True
        assert result.items_before == 2
        assert result.items_healed == 2
        assert result.breakage_kind == "fields"

    def test_unhealable_case_is_a_miss_not_an_error(self, config: SourceConfig) -> None:
        result = self._evaluate(config, UNHEALABLE, expect=False)
        assert result.healed is False
        assert result.matches_expectation is True

    def test_expectation_mismatch_is_visible(self, config: SourceConfig) -> None:
        result = self._evaluate(config, UNHEALABLE, expect=True)
        assert result.healed is False
        assert result.matches_expectation is False

    def test_before_fixture_that_extracts_nothing_is_rejected(self, config: SourceConfig) -> None:
        with pytest.raises(InvalidEvalCaseError, match="before"):
            evaluate_case(
                name="demo",
                description="unit",
                config=config,
                before_html="<html><body></body></html>",
                after_html=FIELD_BROKEN,
                min_items=1,
                expect_healable=True,
            )

    def test_before_fixture_with_a_broken_field_is_rejected(self, config: SourceConfig) -> None:
        with pytest.raises(InvalidEvalCaseError, match="before"):
            evaluate_case(
                name="demo",
                description="unit",
                config=config,
                before_html=FIELD_BROKEN,
                after_html=FIELD_BROKEN,
                min_items=1,
                expect_healable=True,
            )

    def test_after_fixture_that_is_not_broken_is_rejected(self, config: SourceConfig) -> None:
        """A case whose 'after' still works would inflate the heal rate for free."""
        with pytest.raises(InvalidEvalCaseError, match="after"):
            evaluate_case(
                name="demo",
                description="unit",
                config=config,
                before_html=HEALTHY,
                after_html=HEALTHY,
                min_items=1,
                expect_healable=True,
            )


class TestHealRateReport:
    def _result(self, name: str, healed: bool) -> CaseResult:
        return CaseResult(
            name=name,
            description="",
            breakage_kind="fields",
            broken_fields=("title",),
            items_before=2,
            items_broken=2,
            items_healed=2 if healed else 0,
            healed=healed,
            expect_healable=healed,
            proposals=(),
        )

    def test_heal_rate_is_healed_over_total(self) -> None:
        report = HealRateReport(
            cases=(
                self._result("a", True),
                self._result("b", True),
                self._result("c", False),
            )
        )
        assert (report.healed, report.total) == (2, 3)
        assert report.heal_rate == pytest.approx(2 / 3)
        assert report.heal_rate_pct == 66.67

    def test_expectations_met_flag(self) -> None:
        good = HealRateReport(cases=(self._result("a", True),))
        assert good.expectation_failures == ()

        mismatched = CaseResult(
            name="x",
            description="",
            breakage_kind="fields",
            broken_fields=(),
            items_before=1,
            items_broken=1,
            items_healed=0,
            healed=False,
            expect_healable=True,
            proposals=(),
        )
        assert HealRateReport(cases=(mismatched,)).expectation_failures == ("x",)

    def test_empty_suite_is_rejected_rather_than_scoring_zero(self) -> None:
        with pytest.raises(EmptyEvalSuiteError):
            HealRateReport(cases=())


class TestLoadCases:
    def test_reads_the_manifest_and_resolves_paths(self, tmp_path: Path) -> None:
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "demo.yaml").write_text(CONFIG_YAML, encoding="utf-8")
        (tmp_path / "before.html").write_text(HEALTHY, encoding="utf-8")
        (tmp_path / "after.html").write_text(FIELD_BROKEN, encoding="utf-8")
        manifest = tmp_path / "cases.yaml"
        manifest.write_text(
            yaml.safe_dump(
                {
                    "cases": [
                        {
                            "name": "demo",
                            "description": "class rename",
                            "config": "configs/demo.yaml",
                            "before": "before.html",
                            "after": "after.html",
                            "min_items": 2,
                            "expect_healable": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        cases = load_cases(manifest, root=tmp_path)
        assert len(cases) == 1
        assert cases[0].name == "demo"
        assert cases[0].min_items == 2
        assert cases[0].before_path == tmp_path / "before.html"

        report = run_suite(cases)
        assert report.total == 1
        assert report.healed == 1

    def test_manifest_without_cases_is_rejected(self, tmp_path: Path) -> None:
        manifest = tmp_path / "cases.yaml"
        manifest.write_text("cases: []\n", encoding="utf-8")
        with pytest.raises(InvalidEvalCaseError):
            load_cases(manifest, root=tmp_path)

    def test_missing_fixture_is_reported_with_its_path(self, tmp_path: Path) -> None:
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "demo.yaml").write_text(CONFIG_YAML, encoding="utf-8")
        (tmp_path / "after.html").write_text(FIELD_BROKEN, encoding="utf-8")
        manifest = tmp_path / "cases.yaml"
        manifest.write_text(
            yaml.safe_dump(
                {
                    "cases": [
                        {
                            "name": "demo",
                            "config": "configs/demo.yaml",
                            "before": "nope.html",
                            "after": "after.html",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(InvalidEvalCaseError, match=re.escape("nope.html")):
            load_cases(manifest, root=tmp_path)
