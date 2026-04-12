"""Validate a CSS selector against an HTML snapshot."""

from parsel import Selector


def validate_selector(html: str, css_selector: str) -> list[str]:
    """Run a CSS selector against HTML and return matching text values.

    Returns an empty list if no elements match.
    """
    sel = Selector(text=html)
    # Try the selector as-is (may include ::text or ::attr pseudo-elements)
    results = sel.css(css_selector).getall()
    return results
