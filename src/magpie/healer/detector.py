"""Detect whether a scrape run needs healing."""


def should_heal(*, item_count: int, min_items: int) -> bool:
    """Return True if the run produced fewer items than the health threshold.

    Never triggers if min_items is 0 (healing disabled for this source).
    """
    if min_items == 0:
        return False
    return item_count < min_items
