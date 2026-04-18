"""Heal orchestrator — decides PR vs in-DB patch per source origin.

Called from the queue task ``heal_source_task`` and the CLI ``magpie.healer.run``.
Scope is deliberately narrow: fix selectors whose current form returns zero
matches against the live HTML. Anything more sophisticated (selector drift,
partial matches, pagination healing) is deferred.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from magpie.config.schema import SourceConfig
from magpie.healer.github_pr import create_heal_pr
from magpie.healer.selector_fixer import fix_selector
from magpie.healer.validator import validate_selector
from magpie.storage.heals_repo import HealsRepository
from magpie.storage.models import HealMode, SourceOrigin
from magpie.storage.sources_repo import SourcesRepository

log = logging.getLogger("magpie.healer")


async def _fetch_html(url: str) -> str:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def _patched_yaml(
    *,
    original_config: SourceConfig,
    field_name: str,
    new_selector: str,
) -> str:
    """Produce YAML for ``original_config`` with one field's selector replaced."""
    data = original_config.model_dump(mode="json")
    for field in data["item"]["fields"]:
        if field["name"] == field_name:
            field["selector"] = new_selector
            break
    return yaml.safe_dump(data, sort_keys=False)


async def heal_source(
    *,
    source: str,
    run_id: uuid.UUID | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Attempt to heal every broken field on ``source``.

    Writes one ``heals`` row per field attempted (successful or not) and,
    depending on origin, either opens a PR against the committed YAML or
    updates the DB row in place. Returns a summary dict that the queue task
    can log.
    """
    async with session_factory() as session:
        repo = SourcesRepository(session)
        row = await repo.get_by_name(source)
        if row is None:
            return {"source": source, "healed": [], "error": "source not found"}
        config = await repo.get_config(source)
        origin = row.origin
        source_id = row.id

    try:
        html = await _fetch_html(str(config.url))
    except Exception as exc:
        log.exception("Heal fetch failed for %s", source)
        return {"source": source, "healed": [], "error": f"fetch failed: {exc}"}

    healed: list[dict[str, Any]] = []
    patched_yaml = None  # only built if we end up writing a fix

    for field in config.item.fields:
        current_matches = validate_selector(html, field.selector, field.selector_type)
        if current_matches:
            # Selector still works; nothing to heal.
            continue

        try:
            proposal = await fix_selector(
                field_name=field.name,
                old_selector=field.selector,
                html=html,
                old_samples=[],
                selector_type=field.selector_type,
            )
        except Exception as exc:
            log.exception("LLM fix_selector raised for %s.%s", source, field.name)
            await _record_heal(
                session_factory=session_factory,
                source_id=source_id,
                run_id=run_id,
                field_name=field.name,
                old_selector=field.selector,
                new_selector="",
                selector_type=field.selector_type,
                confidence=0.0,
                reasoning=f"LLM error: {exc}",
                sample_values=[],
                mode=HealMode.pr if origin is SourceOrigin.file else HealMode.db_patch,
                pr_url=None,
                applied=False,
            )
            continue

        if not proposal or not proposal.get("selector"):
            continue
        new_selector = proposal["selector"]
        samples = validate_selector(html, new_selector, field.selector_type)
        if not samples:
            await _record_heal(
                session_factory=session_factory,
                source_id=source_id,
                run_id=run_id,
                field_name=field.name,
                old_selector=field.selector,
                new_selector=new_selector,
                selector_type=field.selector_type,
                confidence=float(proposal.get("confidence", 0.0)),
                reasoning=str(proposal.get("reasoning", "")),
                sample_values=list(proposal.get("sample_values", [])),
                mode=HealMode.pr if origin is SourceOrigin.file else HealMode.db_patch,
                pr_url=None,
                applied=False,
            )
            continue

        if origin is SourceOrigin.file:
            pr_url = await create_heal_pr(
                source_name=source,
                field_name=field.name,
                old_selector=field.selector,
                new_selector=new_selector,
                confidence=float(proposal.get("confidence", 0.0)),
                reasoning=str(proposal.get("reasoning", "")),
                sample_values=list(proposal.get("sample_values", [])),
            )
            await _record_heal(
                session_factory=session_factory,
                source_id=source_id,
                run_id=run_id,
                field_name=field.name,
                old_selector=field.selector,
                new_selector=new_selector,
                selector_type=field.selector_type,
                confidence=float(proposal.get("confidence", 0.0)),
                reasoning=str(proposal.get("reasoning", "")),
                sample_values=list(proposal.get("sample_values", [])),
                mode=HealMode.pr,
                pr_url=pr_url,
                applied=False,
            )
            healed.append({"field": field.name, "mode": "pr", "pr_url": pr_url})
        else:
            patched_yaml = _patched_yaml(
                original_config=config,
                field_name=field.name,
                new_selector=new_selector,
            )
            async with session_factory() as session:
                repo = SourcesRepository(session)
                updated_cfg = SourceConfig(**yaml.safe_load(patched_yaml))
                await repo.update_config(
                    name=source,
                    config=updated_cfg,
                    yaml_text=patched_yaml,
                )
                await session.commit()
            config = updated_cfg  # subsequent fields work against updated config
            await _record_heal(
                session_factory=session_factory,
                source_id=source_id,
                run_id=run_id,
                field_name=field.name,
                old_selector=field.selector,
                new_selector=new_selector,
                selector_type=field.selector_type,
                confidence=float(proposal.get("confidence", 0.0)),
                reasoning=str(proposal.get("reasoning", "")),
                sample_values=list(proposal.get("sample_values", [])),
                mode=HealMode.db_patch,
                pr_url=None,
                applied=True,
            )
            healed.append({"field": field.name, "mode": "db_patch", "applied": True})

    return {"source": source, "origin": origin.value, "healed": healed}


async def _record_heal(
    *,
    session_factory: async_sessionmaker[AsyncSession],
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
) -> None:
    async with session_factory() as session:
        repo = HealsRepository(session)
        await repo.create(
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
        await session.commit()


__all__ = ["heal_source"]
