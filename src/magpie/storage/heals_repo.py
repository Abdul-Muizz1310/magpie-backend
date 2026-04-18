"""CRUD for Heal rows — one row per healer attempt, successful or not."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from magpie.storage.models import Heal, HealMode, Source


class HealsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        source_id: uuid.UUID,
        run_id: uuid.UUID | None,
        field_name: str,
        old_selector: str,
        new_selector: str,
        selector_type: str,
        confidence: float,
        reasoning: str,
        sample_values: list[str],
        mode: HealMode,
        pr_url: str | None,
        applied: bool,
    ) -> Heal:
        heal = Heal(
            source_id=source_id,
            run_id=run_id,
            field_name=field_name,
            old_selector=old_selector,
            new_selector=new_selector,
            selector_type=selector_type,
            confidence=confidence,
            reasoning=reasoning,
            sample_values=sample_values,
            mode=mode,
            pr_url=pr_url,
            applied=applied,
        )
        self._session.add(heal)
        await self._session.flush()
        return heal

    async def list_for_source(self, source_id: uuid.UUID) -> Sequence[Heal]:
        stmt = select(Heal).where(Heal.source_id == source_id).order_by(desc(Heal.created_at))
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_all_with_source(
        self,
        *,
        source_name: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[tuple[Heal, str]]:
        """Return heals paired with their source name in a single query.

        The ``/heals`` viewer endpoint used to re-fetch Source per heal
        (classic N+1); joining here keeps the query count at one.
        """
        stmt = (
            select(Heal, Source.name)
            .join(Source, Heal.source_id == Source.id)
            .order_by(desc(Heal.created_at))
            .limit(limit)
            .offset(offset)
        )
        if source_name is not None:
            stmt = stmt.where(Source.name == source_name)
        result = await self._session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]
