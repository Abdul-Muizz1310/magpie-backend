"""Heal orchestrator — decides PR vs in-DB patch per source origin.

Called from the queue task ``heal_source_task`` and the CLI ``magpie.healer.run``.

Detection runs the same extractor the scraper does (``_extract_items_from_html``)
so "broken" means what the scraper sees, not what a detached selector test
thinks. On 0 items we heal the container; on non-zero items with all-``None``
values for some field we heal that field. Fixes for file-origin sources are
recorded in ``heals`` (and an informational PR is opened); fixes for api-origin
sources are additionally written back to ``sources.config_yaml``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from magpie.config.schema import SelectorType, SourceConfig
from magpie.healer.github_pr import create_heal_pr
from magpie.healer.selector_fixer import fix_selector
from magpie.healer.validator import validate_selector
from magpie.scrapy.factory import USER_AGENT, _extract_items_from_html
from magpie.storage.heals_repo import HealsRepository
from magpie.storage.models import HealMode, SourceOrigin
from magpie.storage.sources_repo import SourcesRepository

log = logging.getLogger("magpie.healer")

CONTAINER_TARGET = "container"


async def _fetch_html(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def _patched_yaml(
    *,
    original_config: SourceConfig,
    target: str,
    new_selector: str,
) -> str:
    """Produce YAML with one container or field selector replaced.

    ``target`` is either the literal string ``"container"`` or a field name.
    Other config keys are preserved in-place via ``model_dump``.
    """
    data = original_config.model_dump(mode="json")
    if target == CONTAINER_TARGET:
        data["item"]["container"] = new_selector
    else:
        for field in data["item"]["fields"]:
            if field["name"] == target:
                field["selector"] = new_selector
                break
    return yaml.safe_dump(data, sort_keys=False)


def _broken_field_names(*, config: SourceConfig, raw_items: list[dict[str, Any]]) -> list[str]:
    """Return field names whose value is ``None`` across *every* extracted item."""
    if not raw_items:
        return []
    broken: list[str] = []
    for field in config.item.fields:
        if all(item.get(field.name) is None for item in raw_items):
            broken.append(field.name)
    return broken


async def heal_source(
    *,
    source: str,
    run_id: uuid.UUID | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Attempt to heal every broken field on ``source``.

    Returns a summary dict that the caller (queue task or CLI) can log.
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

    raw_items = _extract_items_from_html(html, config)
    healed: list[dict[str, Any]] = []

    # ── Container healing ────────────────────────────────────────────────
    if not raw_items:
        maybe_config = await _heal_target(
            session_factory=session_factory,
            source_id=source_id,
            run_id=run_id,
            origin=origin,
            source_name=source,
            config=config,
            target=CONTAINER_TARGET,
            old_selector=config.item.container,
            selector_type=config.item.container_type,
            html=html,
            healed_log=healed,
        )
        if maybe_config is not None:
            config = maybe_config
            # Re-extract with the new container to discover field-level issues
            # (if any) in the same heal session.
            raw_items = _extract_items_from_html(html, config)

    # ── Field healing ────────────────────────────────────────────────────
    for field_name in _broken_field_names(config=config, raw_items=raw_items):
        field = next(f for f in config.item.fields if f.name == field_name)
        maybe_config = await _heal_target(
            session_factory=session_factory,
            source_id=source_id,
            run_id=run_id,
            origin=origin,
            source_name=source,
            config=config,
            target=field.name,
            old_selector=field.selector,
            selector_type=field.selector_type,
            html=html,
            healed_log=healed,
        )
        if maybe_config is not None:
            config = maybe_config

    return {"source": source, "origin": origin.value, "healed": healed}


async def _heal_target(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    run_id: uuid.UUID | None,
    origin: SourceOrigin,
    source_name: str,
    config: SourceConfig,
    target: str,
    old_selector: str,
    selector_type: SelectorType,
    html: str,
    healed_log: list[dict[str, Any]],
) -> SourceConfig | None:
    """Heal a single container/field target; returns the new config if applied.

    Records a ``heals`` row on every attempt (even failed ones) so the caller
    can always trace what the LLM tried.
    """
    mode = HealMode.pr if origin is SourceOrigin.file else HealMode.db_patch

    try:
        proposal = await fix_selector(
            field_name=target,
            old_selector=old_selector,
            html=html,
            old_samples=[],
            selector_type=selector_type,
        )
    except Exception as exc:
        log.exception("LLM fix_selector raised for %s.%s", source_name, target)
        await _record_heal(
            session_factory=session_factory,
            source_id=source_id,
            run_id=run_id,
            field_name=target,
            old_selector=old_selector,
            new_selector="",
            selector_type=selector_type,
            confidence=0.0,
            reasoning=f"LLM error: {exc}",
            sample_values=[],
            mode=mode,
            pr_url=None,
            applied=False,
        )
        return None

    if not proposal or not proposal.get("selector"):
        return None

    new_selector = str(proposal["selector"])
    samples = validate_selector(html, new_selector, selector_type)
    if not samples:
        await _record_heal(
            session_factory=session_factory,
            source_id=source_id,
            run_id=run_id,
            field_name=target,
            old_selector=old_selector,
            new_selector=new_selector,
            selector_type=selector_type,
            confidence=float(proposal.get("confidence", 0.0)),
            reasoning=str(proposal.get("reasoning", "")),
            sample_values=list(proposal.get("sample_values", [])),
            mode=mode,
            pr_url=None,
            applied=False,
        )
        return None

    pr_url: str | None = None
    applied = False
    new_config: SourceConfig | None = None

    if origin is SourceOrigin.file:
        pr_url = await create_heal_pr(
            source_name=source_name,
            field_name=target,
            old_selector=old_selector,
            new_selector=new_selector,
            confidence=float(proposal.get("confidence", 0.0)),
            reasoning=str(proposal.get("reasoning", "")),
            sample_values=list(proposal.get("sample_values", [])),
        )
    else:
        patched = _patched_yaml(original_config=config, target=target, new_selector=new_selector)
        new_config = SourceConfig(**yaml.safe_load(patched))
        async with session_factory() as session:
            await SourcesRepository(session).update_config(
                name=source_name,
                config=new_config,
                yaml_text=patched,
            )
            await session.commit()
        applied = True

    await _record_heal(
        session_factory=session_factory,
        source_id=source_id,
        run_id=run_id,
        field_name=target,
        old_selector=old_selector,
        new_selector=new_selector,
        selector_type=selector_type,
        confidence=float(proposal.get("confidence", 0.0)),
        reasoning=str(proposal.get("reasoning", "")),
        sample_values=list(proposal.get("sample_values", [])),
        mode=mode,
        pr_url=pr_url,
        applied=applied,
    )
    healed_log.append(
        {
            "target": target,
            "mode": mode.value,
            "applied": applied,
            "pr_url": pr_url,
            "new_selector": new_selector,
        }
    )
    return new_config


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
