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
    """Interact with the GitHub API to create a heal PR.

    Flow:
        1. Filter existing open PRs with ``head=owner:heal/{source}``.
        2. If found → PATCH title + body (an earlier heal may have been for
           a different field) and re-apply the label (idempotent).
        3. Otherwise → POST a new PR, then POST the label.

    The ``head`` param requires ``owner:branch`` form; passing a bare branch
    silently returns zero matches and causes duplicate PRs.
    """
    token = os.environ.get("GITHUB_PAT_SCRAPE_HEALER", "")
    repo = os.environ.get("GITHUB_REPO", "Abdul-Muizz1310/magpie-backend")
    label = os.environ.get("GITHUB_HEAL_LABEL", "scrape:self-heal")
    owner = repo.split("/", 1)[0]
    branch = f"heal/{source_name}"

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
        resp = await client.get(
            f"/repos/{repo}/pulls",
            headers=headers,
            params={"state": "open", "head": f"{owner}:{branch}"},
        )
        if resp.status_code == 200:
            existing = resp.json()
            if existing:
                pr_number = existing[0]["number"]
                await client.patch(
                    f"/repos/{repo}/pulls/{pr_number}",
                    headers=headers,
                    json={"title": pr_title, "body": pr_body},
                )
                await _apply_label(
                    client=client,
                    headers=headers,
                    repo=repo,
                    pr_number=pr_number,
                    label=label,
                )
                result: dict[str, Any] = existing[0]
                return result

        resp = await client.post(
            f"/repos/{repo}/pulls",
            headers=headers,
            json={
                "title": pr_title,
                "body": pr_body,
                "head": branch,
                "base": "main",
            },
        )
        if resp.status_code in (200, 201):
            data: dict[str, Any] = resp.json()
            pr_number = data["number"]
            await _apply_label(
                client=client,
                headers=headers,
                repo=repo,
                pr_number=pr_number,
                label=label,
            )
            return data

    return None


async def _apply_label(
    *,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    repo: str,
    pr_number: int,
    label: str,
) -> None:
    """Attach ``label`` to PR #``pr_number`` via the issues-labels endpoint.

    PRs are issues on GitHub's data model, so labels live under ``/issues``
    not ``/pulls``. The POST is additive and idempotent — calling it twice
    with the same label is a no-op on the server.
    """
    await client.post(
        f"/repos/{repo}/issues/{pr_number}/labels",
        headers=headers,
        json={"labels": [label]},
    )
