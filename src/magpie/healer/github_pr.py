"""Create GitHub PRs for healed selectors.

The flagship self-heal flow: when a file-origin source's selector drifts, the
healer commits the patched ``configs/{source}.yaml`` onto a ``heal/{source}``
branch via the **Git Data API** (blob → tree → commit → ref) and then opens (or
updates) a pull request against ``base``. GitHub's Create-PR endpoint requires
the head branch to already exist with a commit ahead of base, so the branch/
commit creation must happen first — opening a PR without it 422s on every fresh
source (which is the bug this module previously shipped).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("magpie.healer.github_pr")

_OK = (200, 201)


async def create_heal_pr(
    *,
    source_name: str,
    field_name: str,
    old_selector: str,
    new_selector: str,
    confidence: float,
    reasoning: str,
    sample_values: list[str],
    file_path: str,
    new_content: str,
) -> str | None:
    """Commit the patched config to ``heal/{source}`` and open/update a PR.

    ``file_path`` is the repo-relative path of the YAML to patch (e.g.
    ``configs/hackernews.yaml``) and ``new_content`` is its full patched text.
    Returns the PR URL on success, None on any failure.
    """
    result = await _github_api(
        source_name=source_name,
        field_name=field_name,
        old_selector=old_selector,
        new_selector=new_selector,
        confidence=confidence,
        reasoning=reasoning,
        sample_values=sample_values,
        file_path=file_path,
        new_content=new_content,
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
    file_path: str,
    new_content: str,
) -> dict[str, Any] | None:
    """Commit the patched config to a branch, then create/update the PR.

    Flow:
        1. Commit ``new_content`` at ``file_path`` onto ``heal/{source}`` via the
           Git Data API (creates the branch if absent, updates it if present).
        2. Filter existing open PRs with ``head=owner:heal/{source}``.
        3. If found → PATCH title + body; otherwise → POST a new PR.
        4. (Re-)apply the label — idempotent.
    """
    token = os.environ.get("GITHUB_PAT_SCRAPE_HEALER", "")
    repo = os.environ.get("GITHUB_REPO", "Abdul-Muizz1310/magpie-backend")
    label = os.environ.get("GITHUB_HEAL_LABEL", "scrape:self-heal")
    base = os.environ.get("GITHUB_BASE_BRANCH", "main")
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
    commit_message = f"heal({source_name}): update {field_name} selector\n\n{reasoning}".strip()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(base_url="https://api.github.com") as client:
        committed = await _commit_config_to_branch(
            client=client,
            headers=headers,
            repo=repo,
            base=base,
            branch=branch,
            file_path=file_path,
            new_content=new_content,
            commit_message=commit_message,
        )
        if not committed:
            log.warning("heal branch/commit creation failed for %s; not opening PR", source_name)
            return None

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
                "base": base,
            },
        )
        if resp.status_code in _OK:
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


async def _commit_config_to_branch(
    *,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    repo: str,
    base: str,
    branch: str,
    file_path: str,
    new_content: str,
    commit_message: str,
) -> bool:
    """Commit ``new_content`` at ``file_path`` onto ``branch`` via the Git Data API.

    Creates the branch off ``base`` if it doesn't exist, or fast-forwards it (with
    ``force``) if a prior heal already opened it. Returns True on success.
    """
    # 1. Resolve the base branch tip commit SHA.
    resp = await client.get(f"/repos/{repo}/git/ref/heads/{base}", headers=headers)
    if resp.status_code != 200:
        log.warning("could not read base ref %s: %s", base, resp.status_code)
        return False
    base_sha = resp.json()["object"]["sha"]

    # 2. Base commit -> its tree SHA (so we only replace one file).
    resp = await client.get(f"/repos/{repo}/git/commits/{base_sha}", headers=headers)
    if resp.status_code != 200:
        return False
    base_tree_sha = resp.json()["tree"]["sha"]

    # 3. Blob with the patched file content.
    resp = await client.post(
        f"/repos/{repo}/git/blobs",
        headers=headers,
        json={"content": new_content, "encoding": "utf-8"},
    )
    if resp.status_code not in _OK:
        return False
    blob_sha = resp.json()["sha"]

    # 4. Tree that swaps in the new blob at file_path.
    resp = await client.post(
        f"/repos/{repo}/git/trees",
        headers=headers,
        json={
            "base_tree": base_tree_sha,
            "tree": [{"path": file_path, "mode": "100644", "type": "blob", "sha": blob_sha}],
        },
    )
    if resp.status_code not in _OK:
        return False
    tree_sha = resp.json()["sha"]

    # 5. Commit pointing at the new tree, parented on base.
    resp = await client.post(
        f"/repos/{repo}/git/commits",
        headers=headers,
        json={"message": commit_message, "tree": tree_sha, "parents": [base_sha]},
    )
    if resp.status_code not in _OK:
        return False
    commit_sha = resp.json()["sha"]

    # 6. Create the branch ref, or update it if it already exists (422).
    resp = await client.post(
        f"/repos/{repo}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
    )
    if resp.status_code in _OK:
        return True
    if resp.status_code == 422:
        # Branch already exists (spec'd failure case) — fast-forward it.
        resp = await client.patch(
            f"/repos/{repo}/git/refs/heads/{branch}",
            headers=headers,
            json={"sha": commit_sha, "force": True},
        )
        return resp.status_code == 200
    log.warning("could not create branch ref %s: %s", branch, resp.status_code)
    return False


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
