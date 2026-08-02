"""Spec 07 — offline (network-free) selector re-derivation."""

from __future__ import annotations

import pytest

from magpie.evals.offline_proposer import (
    Accessor,
    normalize_value,
    parse_accessor,
    propose_container_selector,
    propose_field_selector,
)

# ``span.titleline`` renamed to ``span.storylink`` — the shipped fixture pair's break.
CLASS_RENAMED = """\
<html><body><table>
  <tr class="athing" id="1">
    <td class="title"><span class="storylink"><a href="/a1">Alpha</a></span></td>
  </tr>
  <tr class="athing" id="2">
    <td class="title"><span class="storylink"><a href="/a2">Beta</a></span></td>
  </tr>
</table></body></html>
"""

# The ``href`` attribute moved to ``data-href``.
ATTR_RENAMED = """\
<html><body>
  <div class="card" data-sku="p1"><h3 class="n">Widget Alpha</h3>
    <a class="l" data-href="/products/p1">Details</a></div>
  <div class="card" data-sku="p2"><h3 class="n">Widget Beta</h3>
    <a class="l" data-href="/products/p2">Details</a></div>
</body></html>
"""

# An extra inline wrapper defeats any ``a > b`` direct-child chain.
WRAPPER_INSERTED = """\
<html><body><table>
  <tr class="athing" id="1">
    <td class="title"><span class="titleline"><span class="wrap"><a href="/a1">Alpha</a></span></span></td>
  </tr>
  <tr class="athing" id="2">
    <td class="title"><span class="titleline"><span class="wrap"><a href="/a2">Beta</a></span></span></td>
  </tr>
</table></body></html>
"""

# ``tr.athing`` renamed to ``tr.story-row`` — a container break.
CONTAINER_RENAMED = """\
<html><body><table>
  <tr class="story-row" id="1">
    <td class="title"><span class="titleline"><a href="/a1">Alpha</a></span></td>
  </tr>
  <tr class="story-row" id="2">
    <td class="title"><span class="titleline"><a href="/a2">Beta</a></span></td>
  </tr>
</table></body></html>
"""


class TestNormalizeValue:
    def test_collapses_whitespace_and_strips(self) -> None:
        assert normalize_value("  Alpha \n  Beta ") == "Alpha Beta"

    def test_applies_unicode_nfc(self) -> None:
        # "é" as e + combining acute must compare equal to precomposed "é".
        assert normalize_value("é") == normalize_value("é")


class TestParseAccessor:
    def test_text_pseudo_element(self) -> None:
        assert parse_accessor("span.titleline > a::text") == (
            "span.titleline > a",
            Accessor("text", None),
        )

    def test_attr_pseudo_element(self) -> None:
        assert parse_accessor("a::attr(href)") == ("a", Accessor("attr", "href"))

    def test_container_relative_attr_has_empty_fragment(self) -> None:
        assert parse_accessor("::attr(id)") == ("", Accessor("attr", "id"))

    def test_bare_selector_defaults_to_text(self) -> None:
        assert parse_accessor("h3.product-name") == ("h3.product-name", Accessor("text", None))


class TestProposeFieldSelector:
    def test_recovers_from_a_wrapper_class_rename(self) -> None:
        proposed = propose_field_selector(
            html=CLASS_RENAMED,
            container="tr.athing",
            container_type="css",
            old_selector="span.titleline > a::text",
            old_samples=["Alpha", "Beta"],
        )
        assert proposed is not None
        assert proposed.endswith("::text")
        # It must actually work, whatever fragment it chose.
        from magpie.healer.validator import validate_selector

        assert validate_selector(CLASS_RENAMED, proposed) != []

    def test_recovers_a_renamed_attribute(self) -> None:
        proposed = propose_field_selector(
            html=ATTR_RENAMED,
            container="div.card",
            container_type="css",
            old_selector="a.l::attr(href)",
            old_samples=["/products/p1", "/products/p2"],
        )
        assert proposed is not None
        assert proposed.endswith("::attr(data-href)")

    def test_recovers_a_field_read_off_the_container_itself(self) -> None:
        proposed = propose_field_selector(
            html=ATTR_RENAMED,
            container="div.card",
            container_type="css",
            old_selector="::attr(data-id)",
            old_samples=["p1", "p2"],
        )
        assert proposed == "::attr(data-sku)"

    def test_recovers_when_a_wrapper_element_is_inserted(self) -> None:
        proposed = propose_field_selector(
            html=WRAPPER_INSERTED,
            container="tr.athing",
            container_type="css",
            old_selector="span.titleline > a::text",
            old_samples=["Alpha", "Beta"],
        )
        assert proposed is not None
        from magpie.healer.validator import validate_selector

        assert validate_selector(WRAPPER_INSERTED, proposed) != []

    def test_returns_none_when_the_values_are_gone(self) -> None:
        assert (
            propose_field_selector(
                html=CLASS_RENAMED,
                container="tr.athing",
                container_type="css",
                old_selector="span.titleline > a::text",
                old_samples=["Gone Entirely", "Also Gone"],
            )
            is None
        )

    @pytest.mark.parametrize("samples", [[], ["", "  ", "\n"]])
    def test_returns_none_without_usable_samples(self, samples: list[str]) -> None:
        assert (
            propose_field_selector(
                html=CLASS_RENAMED,
                container="tr.athing",
                container_type="css",
                old_selector="a::text",
                old_samples=samples,
            )
            is None
        )

    def test_returns_none_when_the_container_matches_nothing(self) -> None:
        assert (
            propose_field_selector(
                html=CLASS_RENAMED,
                container="tr.does-not-exist",
                container_type="css",
                old_selector="a::text",
                old_samples=["Alpha"],
            )
            is None
        )

    def test_rejects_a_candidate_that_only_resolves_in_a_minority(self) -> None:
        """One anchored row out of five is drift, not a repaired selector."""
        html = (
            "<html><body>"
            + '<div class="row"><span class="only">Alpha</span></div>'
            + '<div class="row"><b>x</b></div>' * 4
            + "</body></html>"
        )
        assert (
            propose_field_selector(
                html=html,
                container="div.row",
                container_type="css",
                old_selector="span.title::text",
                old_samples=["Alpha"],
            )
            is None
        )


class TestProposeContainerSelector:
    def test_recovers_a_renamed_container(self) -> None:
        proposed = propose_container_selector(
            html=CONTAINER_RENAMED,
            samples_by_field={
                "title": ["Alpha", "Beta"],
                "url": ["/a1", "/a2"],
                "id": ["1", "2"],
            },
        )
        assert proposed == "tr.story-row"

    def test_prefers_the_ancestor_covering_the_most_fields(self) -> None:
        """``span.titleline`` repeats too, but only covers title+url — not ``id``."""
        proposed = propose_container_selector(
            html=CONTAINER_RENAMED,
            samples_by_field={"title": ["Alpha", "Beta"], "id": ["1", "2"]},
        )
        assert proposed == "tr.story-row"

    def test_never_proposes_a_signature_that_occurs_once(self) -> None:
        html = '<html><body><div class="wrapper"><p class="solo">Only</p></div></body></html>'
        assert propose_container_selector(html=html, samples_by_field={"t": ["Only"]}) is None

    def test_returns_none_when_no_anchor_matches(self) -> None:
        assert (
            propose_container_selector(
                html=CONTAINER_RENAMED, samples_by_field={"title": ["Nothing Here"]}
            )
            is None
        )

    def test_returns_none_without_any_samples(self) -> None:
        assert propose_container_selector(html=CONTAINER_RENAMED, samples_by_field={}) is None
