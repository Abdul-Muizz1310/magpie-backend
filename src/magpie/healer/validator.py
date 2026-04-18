"""Validate a selector against an HTML snapshot.

Supports both CSS and XPath. A selector is considered valid if it compiles
and — when told to — yields at least one match against the provided HTML.
"""

from __future__ import annotations

from typing import Literal

from parsel import Selector

SelectorType = Literal["css", "xpath"]


def validate_selector(
    html: str,
    selector: str,
    selector_type: SelectorType = "css",
) -> list[str]:
    """Run a selector against HTML and return matching text values.

    Returns an empty list if no elements match — the caller decides whether
    that counts as success or rejection.
    """
    sel = Selector(text=html)
    if selector_type == "xpath":
        return sel.xpath(selector).getall()
    return sel.css(selector).getall()
