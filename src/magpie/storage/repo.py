"""Item repository with content-addressed deduplication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from magpie.core.hashing import compute_item_hash


@dataclass
class PersistResult:
    """Summary of a persist operation."""

    items_new: int
    items_updated: int
    items_removed: int


class ItemRepository:
    """In-memory item repository for deduplication.

    In production this will be backed by Neon Postgres.
    For now, uses an in-memory dict for testing.
    """

    def __init__(self) -> None:
        # source -> dedupe_key -> {hash, data, removed}
        self._store: dict[str, dict[str, dict[str, Any]]] = {}

    def persist_items(
        self,
        source: str,
        items: list[dict[str, Any]],
        *,
        dedupe_key: str,
    ) -> PersistResult:
        """Persist scraped items with deduplication.

        Returns counts of new, updated, and removed items.
        """
        # Validate: all items must have the dedupe_key
        for item in items:
            if dedupe_key not in item:
                msg = f"Item missing dedupe_key '{dedupe_key}': {item}"
                raise ValueError(msg)

        # Check for duplicate dedupe_keys within this batch
        keys = [item[dedupe_key] for item in items]
        if len(keys) != len(set(keys)):
            dupes = [k for k in keys if keys.count(k) > 1]
            msg = f"Duplicate dedupe_keys in batch: {set(dupes)}"
            raise ValueError(msg)

        if source not in self._store:
            self._store[source] = {}

        existing = self._store[source]
        current_keys = set()
        new_count = 0
        updated_count = 0

        for item in items:
            key = str(item[dedupe_key])
            current_keys.add(key)
            item_hash = compute_item_hash(item)

            if key in existing:
                entry = existing[key]
                if entry.get("removed"):
                    # Reappeared
                    entry["removed"] = False
                    entry["hash"] = item_hash
                    entry["data"] = item
                    new_count += 1
                elif entry["hash"] != item_hash:
                    # Updated
                    entry["hash"] = item_hash
                    entry["data"] = item
                    updated_count += 1
                # else: unchanged, just update seen_last (implicit)
            else:
                # New item
                existing[key] = {
                    "hash": item_hash,
                    "data": item,
                    "removed": False,
                }
                new_count += 1

        # Mark removed items
        removed_count = 0
        for key, entry in existing.items():
            if key not in current_keys and not entry.get("removed"):
                entry["removed"] = True
                removed_count += 1

        return PersistResult(
            items_new=new_count,
            items_updated=updated_count,
            items_removed=removed_count,
        )
