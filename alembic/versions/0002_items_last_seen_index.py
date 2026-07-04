"""add composite index on items(source_id, removed, last_seen_at)

Serves the hot viewer/poll queries (list_for_source / list_in_window) which
filter source_id + removed and ORDER BY last_seen_at DESC LIMIT — previously a
filesort over all matching rows per request (MAG-6).

Revision ID: b1c2d3e4f5a6
Revises: fa559bb85a50
Create Date: 2026-07-04 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "fa559bb85a50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_items_source_removed_last_seen",
        "items",
        ["source_id", "removed", "last_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_items_source_removed_last_seen", table_name="items")
