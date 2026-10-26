"""validate_selector: a proposal that does not compile is InvalidSelectorError (spec 03)."""

from __future__ import annotations

import pytest

from magpie.healer.validator import InvalidSelectorError, validate_selector

HTML = "<ul><li class='item'>a</li><li class='item'>b</li></ul>"


def test_a_matching_css_selector_returns_the_values() -> None:
    assert validate_selector(HTML, "li.item::text") == ["a", "b"]


def test_a_matching_xpath_selector_returns_the_values() -> None:
    assert validate_selector(HTML, "//li[@class='item']/text()", "xpath") == ["a", "b"]


def test_a_valid_selector_that_matches_nothing_is_empty() -> None:
    assert validate_selector(HTML, "div.none::text") == []


@pytest.mark.parametrize(
    ("selector", "selector_type", "kind"),
    [("div[", "css", "CSS"), ("li::text(", "css", "CSS"), ("//div[", "xpath", "XPath")],
)
def test_a_selector_that_does_not_compile_is_invalid(
    selector: str, selector_type: str, kind: str
) -> None:
    with pytest.raises(InvalidSelectorError, match=f"is not a valid {kind} selector") as exc:
        validate_selector(HTML, selector, selector_type)  # type: ignore[arg-type]
    assert repr(selector) in str(exc.value)
