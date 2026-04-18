"""Tests for the dual-mode heal orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import yaml

from magpie.config.schema import SourceConfig
from magpie.healer.apply import heal_source
from magpie.storage.heals_repo import HealsRepository
from magpie.storage.models import HealMode, SourceOrigin
from magpie.storage.sources_repo import SourcesRepository

BROKEN_YAML = """\
name: broken
url: https://example.com
schedule: "0 */6 * * *"
item:
  container: "div.card"
  fields:
    - { name: title, selector: ".does-not-exist::text" }
    - { name: id, selector: "::attr(data-id)" }
  dedupe_key: id
"""

HTML_WITH_TITLE_ONLY = """
<html><body>
<div class="card" data-id="1"><h2 class="headline">Hello</h2></div>
<div class="card" data-id="2"><h2 class="headline">World</h2></div>
</body></html>
"""


async def _seed(session_factory, origin: SourceOrigin, name: str = "broken") -> None:
    yaml_text = BROKEN_YAML.replace("broken", name)
    cfg = SourceConfig(**yaml.safe_load(yaml_text))
    async with session_factory() as session:
        await SourcesRepository(session).create(config=cfg, origin=origin, yaml_text=yaml_text)
        await session.commit()


class TestHealApplyApiOrigin:
    async def test_successful_heal_updates_db(self, session_factory) -> None:
        await _seed(session_factory, SourceOrigin.api, name="api-src")

        with (
            patch(
                "magpie.healer.apply._fetch_html",
                new=AsyncMock(return_value=HTML_WITH_TITLE_ONLY),
            ),
            patch(
                "magpie.healer.apply.fix_selector",
                new=AsyncMock(
                    return_value={
                        "selector": "h2.headline::text",
                        "confidence": 0.9,
                        "reasoning": "matches the new markup",
                        "sample_values": ["Hello", "World"],
                    }
                ),
            ),
        ):
            result = await heal_source(
                source="api-src",
                run_id=None,
                session_factory=session_factory,
            )

        assert result["origin"] == "api"
        assert len(result["healed"]) == 1
        assert result["healed"][0]["mode"] == "db_patch"
        assert result["healed"][0]["applied"] is True

        # Source YAML was patched
        async with session_factory() as session:
            row = await SourcesRepository(session).get_by_name("api-src")
            assert row is not None
            assert "h2.headline::text" in row.config_yaml

            heals = await HealsRepository(session).list_for_source(row.id)
            assert len(heals) == 1
            assert heals[0].mode is HealMode.db_patch
            assert heals[0].applied is True


class TestHealApplyFileOrigin:
    async def test_file_origin_opens_pr_no_db_write(self, session_factory) -> None:
        await _seed(session_factory, SourceOrigin.file, name="file-src")

        with (
            patch(
                "magpie.healer.apply._fetch_html",
                new=AsyncMock(return_value=HTML_WITH_TITLE_ONLY),
            ),
            patch(
                "magpie.healer.apply.fix_selector",
                new=AsyncMock(
                    return_value={
                        "selector": "h2.headline::text",
                        "confidence": 0.8,
                        "reasoning": "markup shifted",
                        "sample_values": ["Hello", "World"],
                    }
                ),
            ),
            patch(
                "magpie.healer.apply.create_heal_pr",
                new=AsyncMock(return_value="https://github.com/owner/repo/pull/42"),
            ),
        ):
            result = await heal_source(
                source="file-src",
                run_id=None,
                session_factory=session_factory,
            )

        assert result["origin"] == "file"
        assert result["healed"][0]["mode"] == "pr"
        assert result["healed"][0]["pr_url"] == "https://github.com/owner/repo/pull/42"

        # Source YAML was NOT modified (file-origin stays read-only).
        async with session_factory() as session:
            row = await SourcesRepository(session).get_by_name("file-src")
            assert row is not None
            assert ".does-not-exist::text" in row.config_yaml

            heals = await HealsRepository(session).list_for_source(row.id)
            assert heals[0].mode is HealMode.pr
            assert heals[0].applied is False
            assert heals[0].pr_url == "https://github.com/owner/repo/pull/42"


class TestHealApplyRejectsInvalidProposal:
    async def test_proposal_that_still_yields_zero_records_not_applied(
        self, session_factory
    ) -> None:
        await _seed(session_factory, SourceOrigin.api, name="rejecty")

        with (
            patch(
                "magpie.healer.apply._fetch_html",
                new=AsyncMock(return_value=HTML_WITH_TITLE_ONLY),
            ),
            patch(
                "magpie.healer.apply.fix_selector",
                new=AsyncMock(
                    return_value={
                        "selector": ".still-missing::text",
                        "confidence": 0.3,
                        "reasoning": "guess",
                        "sample_values": [],
                    }
                ),
            ),
        ):
            result = await heal_source(
                source="rejecty",
                run_id=None,
                session_factory=session_factory,
            )

        assert result["healed"] == []
        async with session_factory() as session:
            row = await SourcesRepository(session).get_by_name("rejecty")
            assert row is not None
            heals = await HealsRepository(session).list_for_source(row.id)
            # One heal row recorded with applied=False
            assert len(heals) == 1
            assert heals[0].applied is False

    async def test_no_healing_needed_when_selector_still_matches(self, session_factory) -> None:
        working_yaml = """\
name: still-working
url: https://example.com
schedule: "0 */6 * * *"
item:
  container: "div.card"
  fields:
    - { name: title, selector: "h2::text" }
    - { name: id, selector: "::attr(data-id)" }
  dedupe_key: id
"""
        cfg = SourceConfig(**yaml.safe_load(working_yaml))
        async with session_factory() as session:
            await SourcesRepository(session).create(
                config=cfg, origin=SourceOrigin.api, yaml_text=working_yaml
            )
            await session.commit()

        with (
            patch(
                "magpie.healer.apply._fetch_html",
                new=AsyncMock(return_value=HTML_WITH_TITLE_ONLY),
            ),
            patch("magpie.healer.apply.fix_selector", new=AsyncMock()) as fix,
        ):
            result = await heal_source(
                source="still-working",
                run_id=None,
                session_factory=session_factory,
            )

        assert result["healed"] == []
        fix.assert_not_called()


class TestHealApplyMissingSource:
    async def test_missing_source_returns_error(self, session_factory) -> None:
        result = await heal_source(
            source="ghost",
            run_id=None,
            session_factory=session_factory,
        )
        assert result["healed"] == []
        assert result["error"] == "source not found"


class TestHealApplyContainer:
    """When the container selector returns 0 items, heal the container first."""

    BROKEN_CONTAINER_YAML = """\
name: broken-container
url: https://example.com
schedule: "0 */6 * * *"
item:
  container: "div.old-container-class"
  fields:
    - { name: title, selector: "h2::text" }
    - { name: id, selector: "::attr(data-id)" }
  dedupe_key: id
"""

    HTML = """
    <html><body>
      <article class="new-card" data-id="1"><h2>Alpha</h2></article>
      <article class="new-card" data-id="2"><h2>Beta</h2></article>
    </body></html>
    """

    async def test_container_heal_triggers_when_zero_items(self, session_factory) -> None:
        cfg = SourceConfig(**yaml.safe_load(self.BROKEN_CONTAINER_YAML))
        async with session_factory() as session:
            await SourcesRepository(session).create(
                config=cfg,
                origin=SourceOrigin.api,
                yaml_text=self.BROKEN_CONTAINER_YAML,
            )
            await session.commit()

        with (
            patch(
                "magpie.healer.apply._fetch_html",
                new=AsyncMock(return_value=self.HTML),
            ),
            patch(
                "magpie.healer.apply.fix_selector",
                new=AsyncMock(
                    return_value={
                        "selector": "article.new-card",
                        "confidence": 0.95,
                        "reasoning": "markup now uses <article>",
                        "sample_values": ["Alpha", "Beta"],
                    }
                ),
            ) as mock_fix,
        ):
            result = await heal_source(
                source="broken-container",
                run_id=None,
                session_factory=session_factory,
            )

        # fix_selector was called with target='container' first
        first_call_kwargs = mock_fix.await_args_list[0].kwargs
        assert first_call_kwargs["field_name"] == "container"
        assert first_call_kwargs["old_selector"] == "div.old-container-class"

        # Container was patched in the stored YAML.
        async with session_factory() as session:
            row = await SourcesRepository(session).get_by_name("broken-container")
            assert row is not None
            assert "article.new-card" in row.config_yaml
            assert "div.old-container-class" not in row.config_yaml

        # Summary records the container heal.
        targets = [h["target"] for h in result["healed"]]
        assert "container" in targets
