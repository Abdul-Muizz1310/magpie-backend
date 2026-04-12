"""Create GitHub PRs for healed selectors."""

from __future__ import annotations

import os
from typing import Any

import httpx


async def create_heal_pr(
    *,
    source_name: str,
    field_name: str,
    old_selector: str,
    new_selector: str,
    confidence: float,
    reasoning: str,
    sample_values: list[str],
) -> str | None:
    """Create or update a GitHub PR with the healed selector.

    Returns the PR URL on success, None on failure.
    """
    result = await _github_api(
        source_name=source_name,
        field_name=field_name,
        old_selector=old_selector,
        new_selector=new_selector,
        confidence=confidence,
        reasoning=reasoning,
        sample_values=sample_values,
    )
    if result and "html_url" in result:
        return result["html_url"]  # type: ignore[no-any-return]
    return None


async def _github_api(
    *,
    source_name: str,
    field_name: str,
    old_selector: str,
    new_selector: str,
    confidence: float,
    reasoning: str,
    sample_values: list[str],
) -> dict[str, Any] | None:
    """Interact with the GitHub API to create a heal PR."""
    token = os.environ.get("GITHUB_PAT_SCRAPE_HEALER", "")
    repo = os.environ.get("GITHUB_REPO", "Abdul-Muizz1310/magpie-backend")
    label = os.environ.get("GITHUB_HEAL_LABEL", "scrape:self-heal")

    pr_title = f"heal({source_name}): update {field_name} selector"
    pr_body = (
        f"## Self-Heal: `{source_name}.{field_name}`\n\n"
        f"**Old selector:** `{old_selector}`\n"
        f"**New selector:** `{new_selector}`\n"
        f"**Confidence:** {confidence:.0%}\n\n"
        f"### Reasoning\n{reasoning}\n\n"
        f"### Sample values extracted\n"
        + "\n".join(f"- `{v}`" for v in sample_values)
        + f"\n\n---\n*Label: `{label}`*"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(base_url="https://api.github.com") as client:
        # Check for existing open PR on same source
        resp = await client.get(
            f"/repos/{repo}/pulls",
            headers=headers,
            params={"state": "open", "head": f"heal/{source_name}"},
        )
        if resp.status_code == 200:
            existing = resp.json()
            if existing:
                # Update existing PR
                pr_number = existing[0]["number"]
                await client.patch(
                    f"/repos/{repo}/pulls/{pr_number}",
                    headers=headers,
                    json={"body": pr_body},
                )
                result: dict[str, Any] = existing[0]
                return result

        # Create new PR
        resp = await client.post(
            f"/repos/{repo}/pulls",
            headers=headers,
            json={
                "title": pr_title,
                "body": pr_body,
                "head": f"heal/{source_name}",
                "base": "main",
            },
        )
        if resp.status_code in (200, 201):
            data: dict[str, Any] = resp.json()
            return data

    return None
