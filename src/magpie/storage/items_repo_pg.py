"""Postgres-backed item repository — mirrors the in-memory ``persist_items`` contract.

The dedup logic is identical to ``storage/repo.ItemRepository`` (new / updated
/ removed / reappeared) but sourced from and persisted to the ``items`` table.
Kept in a separate class so tests that want the pure in-memory behaviour can
still import ``storage.repo.ItemRepository`` without a DB.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from magpie.core.hashing import compute_item_hash
from magpie.storage.models import Item
from magpie.storage.repo import PersistResult


class PgItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_items(
        self,
        source_id: uuid.UUID,
        items: list[dict[str, Any]],
        *,
        dedupe_key: str,
    ) -> PersistResult:
        """Persist items with new/updated/removed/reappeared accounting.

        Every item is expected to carry ``dedupe_key``; duplicates within the
        batch are rejected so the unique constraint on
        ``(source_id, dedupe_key)`` never fires mid-transaction.
        """
        for item in items:
            if dedupe_key not in item:
                msg = f"Item missing dedupe_key {dedupe_key!r}: {item}"
                raise ValueError(msg)

        keys = [str(item[dedupe_key]) for item in items]
        if len(keys) != len(set(keys)):
            dupes = {k for k in keys if keys.count(k) > 1}
            msg = f"Duplicate dedupe_keys in batch: {dupes}"
            raise ValueError(msg)

        result = await self._session.execute(select(Item).where(Item.source_id == source_id))
        existing: dict[str, Item] = {row.dedupe_key: row for row in result.scalars().all()}

        now = datetime.now(UTC)
        current_keys: set[str] = set()
        new_count = 0
        updated_count = 0

        for item in items:
            key = str(item[dedupe_key])
            current_keys.add(key)
            item_hash = compute_item_hash(item)

            row = existing.get(key)
            if row is not None:
                if row.removed:
                    row.removed = False
                    row.content_hash = item_hash
                    row.data = item
                    row.last_seen_at = now
                    new_count += 1
                elif row.content_hash != item_hash:
                    row.content_hash = item_hash
                    row.data = item
                    row.last_seen_at = now
                    updated_count += 1
                else:
                    row.last_seen_at = now
            else:
                self._session.add(
                    Item(
                        source_id=source_id,
                        dedupe_key=key,
                        content_hash=item_hash,
                        data=item,
                        removed=False,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                new_count += 1

        removed_count = 0
        for key, row in existing.items():
            if key not in current_keys and not row.removed:
                row.removed = True
                removed_count += 1

        await self._session.flush()
        return PersistResult(
            items_new=new_count,
            items_updated=updated_count,
            items_removed=removed_count,
        )
