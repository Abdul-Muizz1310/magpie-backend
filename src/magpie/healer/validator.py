"""Validate a selector against an HTML snapshot.

Supports both CSS and XPath. A selector is considered valid if it compiles
and — when told to — yields at least one match against the provided HTML.
"""

from __future__ import annotations

from typing import Literal

from cssselect import SelectorError
from parsel import Selector

SelectorType = Literal["css", "xpath"]

_KIND: dict[SelectorType, str] = {"css": "CSS", "xpath": "XPath"}


class InvalidSelectorError(ValueError):
    """A selector that does not compile as the kind it claims to be (spec 03)."""


def validate_selector(
    html: str,
    selector: str,
    selector_type: SelectorType = "css",
) -> list[str]:
    """Run a selector against HTML and return matching text values.

    Returns an empty list if no elements match — the caller decides whether
    that counts as success or rejection. Raises :class:`InvalidSelectorError` when the
    selector does not compile: the healer's selectors are model output, so a
    malformed one is a rejected proposal, not a crash.
    """
    sel = Selector(text=html)
    try:
        if selector_type == "xpath":
            return sel.xpath(selector).getall()
        return sel.css(selector).getall()
    except (SelectorError, ValueError) as exc:
        msg = f"{selector!r} is not a valid {_KIND[selector_type]} selector: {exc}"
        raise InvalidSelectorError(msg) from exc
