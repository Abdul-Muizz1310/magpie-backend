"""Load and validate YAML config files."""

from __future__ import annotations

from pathlib import Path

import yaml

from magpie.config.schema import SourceConfig


def load_config(yaml_string: str) -> SourceConfig:
    """Parse a YAML string into a validated SourceConfig."""
    data = yaml.safe_load(yaml_string)
    if not isinstance(data, dict):
        msg = "YAML must parse to a mapping, got: " + type(data).__name__
        raise ValueError(msg)
    return SourceConfig(**data)


def load_config_from_file(path: Path) -> SourceConfig:
    """Load and validate a SourceConfig from a YAML file."""
    text = path.read_text(encoding="utf-8")
    return load_config(text)
