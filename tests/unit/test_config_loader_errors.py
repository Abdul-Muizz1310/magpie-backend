"""Tests for config/loader.py error branches."""

from pathlib import Path

import pytest

from magpie.config.loader import load_config, load_config_from_file


class TestLoadConfigFromFile:
    def test_nonexistent_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_config_from_file(Path("/nonexistent/path/config.yaml"))


class TestLoadConfigNonMapping:
    def test_yaml_list_raises(self) -> None:
        with pytest.raises(ValueError, match="YAML must parse to a mapping"):
            load_config("- item1\n- item2")

    def test_yaml_scalar_raises(self) -> None:
        with pytest.raises(ValueError, match="YAML must parse to a mapping"):
            load_config("just a string")
