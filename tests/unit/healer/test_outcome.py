"""``HealSummary``: a heal ends healed or failed, never neither (spec 03, 2026-10-20)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from magpie.healer.outcome import HealedTarget, HealFailure, HealSummary
from magpie.storage.models import HealMode, SourceOrigin

_PR = HealedTarget(
    target="container",
    mode=HealMode.pr,
    applied=False,
    pr_url="https://github.com/owner/repo/pull/7",
    new_selector="dl#articles > dd",
)
_LLM_404 = HealFailure(target="container", reason="LLM error: HTTPStatusError: 404 Not Found")


class TestHealSummaryInvariant:
    def test_a_summary_with_neither_healed_nor_failures_cannot_be_built(self) -> None:
        with pytest.raises(ValidationError, match="neither healed nor failed"):
            HealSummary(source="arxiv-cs", origin=SourceOrigin.file, healed=(), failures=())

    def test_healed_only_succeeds(self) -> None:
        summary = HealSummary(source="arxiv-cs", origin=SourceOrigin.file, healed=(_PR,))
        assert summary.succeeded is True

    def test_any_failure_fails_even_beside_a_heal(self) -> None:
        summary = HealSummary(
            source="arxiv-cs",
            origin=SourceOrigin.file,
            healed=(_PR,),
            failures=(HealFailure(target="title", reason="selector rejected: 'x' matched 0"),),
        )
        assert summary.succeeded is False

    def test_failure_only_fails(self) -> None:
        summary = HealSummary(source="arxiv-cs", origin=SourceOrigin.file, failures=(_LLM_404,))
        assert summary.succeeded is False

    def test_unknown_source_has_no_origin(self) -> None:
        summary = HealSummary(
            source="ghost",
            origin=None,
            failures=(HealFailure(target="source", reason="source not found"),),
        )
        assert summary.origin is None
        assert summary.succeeded is False

    def test_summary_is_frozen(self) -> None:
        summary = HealSummary(source="arxiv-cs", origin=SourceOrigin.file, healed=(_PR,))
        with pytest.raises(ValidationError):
            summary.source = "other"  # type: ignore[misc]

    @pytest.mark.parametrize("field", ["target", "reason"])
    def test_failure_fields_must_be_non_empty(self, field: str) -> None:
        values = {"target": "container", "reason": "LLM error: boom"}
        values[field] = ""
        with pytest.raises(ValidationError):
            HealFailure(**values)


class TestHealSummaryDescription:
    def test_describe_names_every_failed_target_and_reason(self) -> None:
        summary = HealSummary(
            source="arxiv-cs",
            origin=SourceOrigin.file,
            failures=(
                _LLM_404,
                HealFailure(target="title", reason="no selector proposed: page has no titles"),
            ),
        )
        text = summary.describe_failures()
        assert "container: LLM error: HTTPStatusError: 404 Not Found" in text
        assert "title: no selector proposed: page has no titles" in text

    def test_json_dump_round_trips_for_the_queue(self) -> None:
        summary = HealSummary(
            source="arxiv-cs", origin=SourceOrigin.file, healed=(_PR,), failures=(_LLM_404,)
        )
        dumped = summary.model_dump(mode="json")
        assert dumped["origin"] == "file"
        assert dumped["healed"][0]["mode"] == "pr"
        assert dumped["failures"][0]["target"] == "container"
        assert HealSummary.model_validate(dumped) == summary
