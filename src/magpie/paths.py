"""Project path resolution — centralised so packaging changes stay local."""

from __future__ import annotations

import os
from pathlib import Path


def _repo_root() -> Path:
    """Return the repo root when running from a source checkout.

    ``src/magpie/paths.py`` → ``parents[2]`` is the repo root.
    """
    return Path(__file__).resolve().parents[2]


def configs_dir() -> Path:
    """Return the source configs directory.

    Honours ``MAGPIE_CONFIGS_DIR`` when set; otherwise falls back to
    ``<repo>/configs``. An env override lets deployments mount configs
    read-only from a different location without touching the code.
    """
    override = os.environ.get("MAGPIE_CONFIGS_DIR")
    if override:
        return Path(override)
    return _repo_root() / "configs"
