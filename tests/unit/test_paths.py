"""Tests for magpie.paths config-dir resolution."""

from __future__ import annotations

from unittest.mock import patch

from magpie.paths import configs_dir


def test_configs_dir_defaults_to_repo_root_configs() -> None:
    """No env override -> configs/ under the repo root."""
    with patch.dict("os.environ", {}, clear=False):
        # Ensure the env var is not set for this test.
        import os

        os.environ.pop("MAGPIE_CONFIGS_DIR", None)
        result = configs_dir()
    assert result.name == "configs"


def test_configs_dir_honours_env_override(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """MAGPIE_CONFIGS_DIR, when set, overrides the default location."""
    override = tmp_path / "custom-configs"
    with patch.dict("os.environ", {"MAGPIE_CONFIGS_DIR": str(override)}):
        result = configs_dir()
    assert result == override
