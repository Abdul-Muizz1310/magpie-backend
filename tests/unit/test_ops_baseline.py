"""Repo-level ops invariants that CI must not be able to silently lose.

These are cheap structural assertions over committed config. They exist because
the failure mode is invisible: a missing ``dependabot.yml`` doesn't break a
build, it just means nothing ever tells us a pinned action or dependency went
stale.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _dependabot() -> dict[str, object]:
    assert DEPENDABOT.is_file(), f"missing {DEPENDABOT.relative_to(REPO_ROOT)}"
    loaded = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "dependabot.yml must parse to a mapping"
    return loaded


def test_dependabot_config_is_schema_valid() -> None:
    """``version: 2`` plus a non-empty ``updates`` list — GitHub rejects anything else."""
    data = _dependabot()
    assert data["version"] == 2
    updates = data["updates"]
    assert isinstance(updates, list) and updates, "updates must be a non-empty list"
    for entry in updates:
        assert isinstance(entry, dict)
        # GitHub requires all three keys on every update entry.
        assert set(entry) >= {"package-ecosystem", "directory", "schedule"}
        assert entry["schedule"]["interval"] == "weekly"


def test_dependabot_covers_actions_and_uv_ecosystems() -> None:
    """Both ecosystems this repo actually consumes must be watched.

    ``github-actions`` covers the three workflows; ``uv`` covers ``uv.lock``
    (the project pins its Python deps there, not in a requirements file).
    """
    ecosystems = {entry["package-ecosystem"] for entry in _dependabot()["updates"]}  # type: ignore[union-attr]
    assert {"github-actions", "uv"} <= ecosystems


def test_ci_docker_build_passes_commit_sha_build_arg() -> None:
    """The Dockerfile's ``ARG COMMIT_SHA`` is useless unless a builder supplies it.

    Without this, every image ships ``COMMIT_SHA=unknown`` baked in and
    ``/version`` reports a placeholder forever.
    """
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "ARG COMMIT_SHA" in DOCKERFILE.read_text(encoding="utf-8")
    assert "--build-arg COMMIT_SHA=" in text, "ci.yml docker build must pass --build-arg COMMIT_SHA"


# ── Cloudflare R2 is planned, not shipped ────────────────────────────────────

R2_ENV_PREFIX = "R2_"
SRC = REPO_ROOT / "src"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _shipped_code_reads_r2() -> set[str]:
    """Every ``R2_*`` environment variable any shipped module actually reads."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        for token in path.read_text(encoding="utf-8").split():
            stripped = token.strip("\"'(),[]{}:").removeprefix("os.environ.get")
            if stripped.startswith(R2_ENV_PREFIX):
                found.add(stripped)
    return found


def test_no_shipped_module_reads_an_r2_variable() -> None:
    """Guards the premise of every "R2 is planned" label in the docs.

    If someone wires the archive, this fails and forces the docs, the spec and
    the workflow env blocks to be updated together — the labels can never
    silently become wrong in the *other* direction either.
    """
    assert _shipped_code_reads_r2() == set(), (
        "R2 is now read by shipped code — un-label it as planned in "
        "docs/specs/01-factory.md, 03-healer.md, 06-batch-scrape.md, "
        "docs/ARCHITECTURE.md, README.md, .env.example and render.yaml"
    )


def test_no_workflow_injects_unused_r2_credentials() -> None:
    """A workflow that exports ``R2_*`` advertises an integration that isn't there.

    It also hands three live credentials to a job that provably cannot use them
    (see the test above), which is surface for nothing. They come back when the
    archive does.
    """
    offenders = {
        workflow.name: [
            line.strip()
            for line in workflow.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(R2_ENV_PREFIX)
        ]
        for workflow in sorted(WORKFLOW_DIR.glob("*.yml"))
    }
    leaking = {name: lines for name, lines in offenders.items() if lines}
    assert leaking == {}, f"workflows export R2_* that nothing reads: {leaking}"
