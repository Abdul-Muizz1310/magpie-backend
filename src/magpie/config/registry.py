"""Discover and load all config files from a directory."""

from __future__ import annotations

from pathlib import Path

from magpie.config.loader import load_config_from_file
from magpie.config.schema import SourceConfig


def load_all_configs(directory: Path) -> list[SourceConfig]:
    """Load all .yaml files from a directory into validated SourceConfig models."""
    configs: list[SourceConfig] = []
    for path in sorted(directory.glob("*.yaml")):
        configs.append(load_config_from_file(path))
    return configs
