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
from magpie.core.safe_fetch import safe_get_async
from magpie.healer.github_pr import create_heal_pr
from magpie.healer.outcome import SOURCE_TARGET, HealedTarget, HealFailure, HealSummary
from magpie.healer.selector_fixer import fix_selector
from magpie.healer.validator import validate_selector
from magpie.scrapy.factory import USER_AGENT, _extract_items_from_html
from magpie.storage.heals_repo import HealsRepository
from magpie.storage.models import HealMode, SourceOrigin
from magpie.storage.sources_repo import SourcesRepository

log = logging.getLogger("magpie.healer")

CONTAINER_TARGET = "container"


async def _fetch_html(config: SourceConfig) -> str:
    """Fetch the source's current HTML for the healer to analyze.

    For ``render: true`` sources we route through Playwright — the same path
    the scraper uses — so anti-bot protections (Product Hunt's 403 on plain
    httpx, Cloudflare challenges, etc.) don't blind the healer. Static
    sources stay on ``httpx`` to keep the happy path fast.
    """
    if config.render:
        from magpie.playwright.runner import PlaywrightRunner

        return await PlaywrightRunner(config).fetch_html()

    # follow_redirects=False + safe_get_async: redirect hosts are re-validated
    # against the SSRF allowlist so a 302 to an internal address isn't followed.
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = await safe_get_async(client, str(config.url))
        resp.raise_for_status()
        return resp.text


def patched_yaml(
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


def broken_field_names(*, config: SourceConfig, raw_items: list[dict[str, Any]]) -> list[str]:
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
) -> HealSummary:
    """Attempt to heal every broken selector on ``source``.

    Returns a :class:`HealSummary` that is either healed or carries the reason it
    is not (spec 03, 2026-10-20). Every failure is logged at ERROR here.
    """
    async with session_factory() as session:
        repo = SourcesRepository(session)
        row = await repo.get_by_name(source)
        if row is None:
            log.error("heal of %s failed: source not found", source)
            return HealSummary(
                source=source,
                origin=None,
                failures=(HealFailure(target=SOURCE_TARGET, reason="source not found"),),
            )
        config = await repo.get_config(source)
        origin = row.origin
        source_id = row.id

    try:
        html = await _fetch_html(config)
    except Exception as exc:
        log.exception("Heal fetch failed for %s", source)
        return HealSummary(
            source=source,
            origin=origin,
            failures=(HealFailure(target=SOURCE_TARGET, reason=f"fetch failed: {exc}"),),
        )

    raw_items = _extract_items_from_html(html, config)
    outcome = _HealLog()

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
            outcome=outcome,
        )
        if maybe_config is not None:
            config = maybe_config
            # Re-extract with the new container to discover field-level issues
            # (if any) in the same heal session.
            raw_items = _extract_items_from_html(html, config)

    # ── Field healing ────────────────────────────────────────────────────
    for field_name in broken_field_names(config=config, raw_items=raw_items):
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
            outcome=outcome,
        )
        if maybe_config is not None:
            config = maybe_config

    if not outcome.healed and not outcome.failures:
        # The run underflowed (or errored) but the live page extracts items with
        # every field populated: nothing here explains it, and nothing was fixed.
        reason = (
            f"nothing to heal: the live page yields {len(raw_items)} items and no field "
            "is empty in all of them"
        )
        outcome.fail(source, SOURCE_TARGET, reason)

    return HealSummary(
        source=source,
        origin=origin,
        healed=tuple(outcome.healed),
        failures=tuple(outcome.failures),
    )


class _HealLog:
    """Accumulates one heal session's outcomes; every failure is logged at ERROR."""

    def __init__(self) -> None:
        self.healed: list[HealedTarget] = []
        self.failures: list[HealFailure] = []

    def fail(self, source_name: str, target: str, reason: str) -> None:
        log.error("heal of %s.%s failed: %s", source_name, target, reason)
        self.failures.append(HealFailure(target=target, reason=reason))


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
    outcome: _HealLog,
) -> SourceConfig | None:
    """Heal a single container/field target; returns the new config if applied.

    Records a ``heals`` row on every attempt that reached the LLM (even failed
    ones) so the caller can always trace what the LLM tried, and adds exactly one
    entry to ``outcome``: healed, or failed with the reason.
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
        outcome.fail(source_name, target, f"LLM error: {type(exc).__name__}: {exc}")
        return None

    if not proposal or not proposal.get("selector"):
        reasoning = str(proposal.get("reasoning", "")).strip() if proposal else ""
        reason = f"no selector proposed: {reasoning}" if reasoning else "no selector proposed"
        outcome.fail(source_name, target, reason)
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
        outcome.fail(
            source_name,
            target,
            f"selector rejected: {new_selector!r} matches nothing on the live page",
        )
        return None

    pr_url: str | None = None
    applied = False
    new_config: SourceConfig | None = None

    if origin is SourceOrigin.file:
        # File-origin sources are healed via PR: commit the patched YAML onto the
        # heal branch and open a PR. The committed file is the real config file
        # under configs/, so the branch actually differs from base and the PR is
        # openable (previously the PR call had no branch/commit and 422'd).
        patched = patched_yaml(original_config=config, target=target, new_selector=new_selector)
        pr_url = await create_heal_pr(
            source_name=source_name,
            field_name=target,
            old_selector=old_selector,
            new_selector=new_selector,
            confidence=float(proposal.get("confidence", 0.0)),
            reasoning=str(proposal.get("reasoning", "")),
            sample_values=list(proposal.get("sample_values", [])),
            file_path=f"configs/{source_name}.yaml",
            new_content=patched,
        )
    else:
        patched = patched_yaml(original_config=config, target=target, new_selector=new_selector)
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
    if mode is HealMode.pr and pr_url is None:
        # The selector validated but no reviewer will ever see it: not healed.
        # github_pr logs which GitHub step failed and why.
        outcome.fail(
            source_name,
            target,
            f"PR not opened for validated selector {new_selector!r} (see the github_pr error)",
        )
        return None

    outcome.healed.append(
        HealedTarget(
            target=target,
            mode=mode,
            applied=applied,
            pr_url=pr_url,
            new_selector=new_selector,
        )
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


__all__ = ["broken_field_names", "heal_source", "patched_yaml"]
